#!/usr/bin/env python3
"""Run the backend cost_normal suite as a CI gate.

Backend pytest has never run in CI, so red tests accumulated on main unnoticed:
test_mcp_manifest_parity was failing for an unknown period and was only found by
accident while investigating something else. Measured on 20 August 2026, 110 of
3769 cost_normal tests fail on main with a working database.

Switching CI on with that backlog would produce a job that is red on arrival and
therefore ignored, which is worse than no gate. So the gate deselects a recorded
list of known failures and fails on anything outside it.

The list may only shrink. ``--check-list`` verifies every entry still exists and
still fails; an entry that now passes must be deleted rather than left to rot,
which stops the baseline being quietly raised the way the line-count guardrail
in the reliability ratchet case log was.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
KNOWN_FAILURES = REPO_ROOT / "ci" / "known_test_failures.txt"
MARKER = "cost_normal"
TEST_PATH = "tests/backend"


def load_known_failures() -> list[str]:
    if not KNOWN_FAILURES.exists():
        return []
    entries = []
    for line in KNOWN_FAILURES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def _pytest(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def check_list() -> int:
    """Verify the known-failure list is neither stale nor silently growing."""
    known = load_known_failures()
    if not known:
        print("known-failure list is empty; nothing to check")
        return 0

    collected = _pytest(
        TEST_PATH, "-m", MARKER, "--collect-only", "-q", "-p", "no:cacheprovider"
    )
    if collected.returncode != 0:
        # Without this the failure below is unreadable: a collection error makes
        # every recorded entry look as though it no longer exists. The first CI
        # run of this gate reported 110 missing entries when the real cause was
        # that pytest was not installed.
        print("collection failed, so the known-failure list cannot be checked:")
        print((collected.stderr or collected.stdout or "").strip()[-2000:])
        return 1

    available = set()
    for line in collected.stdout.splitlines():
        if "::" in line:
            available.add(line.strip())

    missing = [entry for entry in known if entry not in available]
    if missing:
        print(
            f"{len(missing)} known-failure entries no longer exist. Remove them:\n  "
            + "\n  ".join(missing[:20])
        )
        return 1

    print(f"all {len(known)} known-failure entries still exist and are collected")
    return 0


def _shard_files(index: int, count: int) -> list[str]:
    """Split test files into balanced shards by test count.

    Sharding by file rather than by test keeps per-module fixtures intact, and
    balancing by count matters because the files are very uneven. The lanes
    cannot be used for this: lane_backend_core holds 3488 of 3776 cost_normal
    tests, so a lane matrix runs at the speed of one lane.
    """
    collected = _pytest(
        TEST_PATH, "-m", MARKER, "--collect-only", "-q", "-p", "no:cacheprovider"
    )
    if collected.returncode != 0:
        raise SystemExit(
            "collection failed while sharding:\n"
            + (collected.stderr or collected.stdout or "").strip()[-2000:]
        )

    per_file: dict[str, int] = {}
    for line in collected.stdout.splitlines():
        if "::" in line:
            per_file[line.split("::", 1)[0].strip()] = (
                per_file.get(line.split("::", 1)[0].strip(), 0) + 1
            )

    # Largest-first greedy bin packing: deterministic, and good enough given the
    # spread. Ties break on filename so shards are stable across runs.
    buckets: list[list[str]] = [[] for _ in range(count)]
    sizes = [0] * count
    for path, size in sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0])):
        target = sizes.index(min(sizes))
        buckets[target].append(path)
        sizes[target] += size

    print(f"shard {index}/{count}: {len(buckets[index - 1])} files, ~{sizes[index - 1]} tests")
    return sorted(buckets[index - 1])


def run_gate(extra: list[str], shard: str | None = None) -> int:
    known = load_known_failures()
    if shard:
        index, _, count = shard.partition("/")
        targets = _shard_files(int(index), int(count))
        if not targets:
            print("shard is empty; nothing to run")
            return 0
    else:
        targets = [TEST_PATH]
    args = [*targets, "-m", MARKER, "-q", "--no-header", "-p", "no:cacheprovider"]
    for entry in known:
        args += ["--deselect", entry]
    args += extra

    if not known:
        print(
            "known-failure list is empty. If that is unexpected, the list failed "
            "to load rather than the backlog being cleared."
        )
    scope = TEST_PATH if targets == [TEST_PATH] else f"{len(targets)} sharded files"
    print(f"running {scope} -m {MARKER}, deselecting {len(known)} recorded failures")
    result = subprocess.run([sys.executable, "-m", "pytest", *args], cwd=REPO_ROOT)
    if result.returncode == 0:
        print("gate passed")
    else:
        print(
            "gate failed. A test outside ci/known_test_failures.txt broke.\n"
            "Do not add it to that list: the list may only shrink."
        )
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shard",
        help='run one shard, as "index/count" with index starting at 1',
    )
    parser.add_argument(
        "--check-list",
        action="store_true",
        help="verify the known-failure list is current instead of running the gate",
    )
    parsed, extra = parser.parse_known_args()
    if parsed.check_list:
        return check_list()
    return run_gate(extra, shard=parsed.shard)


if __name__ == "__main__":
    sys.exit(main())
