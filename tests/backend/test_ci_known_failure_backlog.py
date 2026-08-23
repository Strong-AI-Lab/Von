"""The CI known-failure list may only shrink.

Backend pytest has never run in CI, so red tests accumulated on main unnoticed.
The gate in .github/workflows/backend-tests.yml deselects a recorded backlog so
it can be switched on without being red on arrival, which would guarantee it
gets ignored.

That deselection is only safe while the list is a ratchet in the useful
direction. The reliability ratchet case log records the opposite outcome: a
guardrail whose accepted baseline was refreshed from 46,106 lines to 49,717
until compliance meant nothing. These tests exist so this list cannot go the
same way.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWN_FAILURES = REPO_ROOT / "ci" / "known_test_failures.txt"

# Reconciled from the 22 August 2026 nightly at ef56c6ed. Of the 209 recorded
# failures, 103 passed and were removed. This number is a ceiling, never a
# target to refresh.
BASELINE_COUNT = 106


def _entries() -> list[str]:
    lines = KNOWN_FAILURES.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def test_the_backlog_never_grows():
    """Adding an entry is not a permitted way to make CI green.

    A newly broken test must be fixed or reverted. If this fails because the
    baseline genuinely needs raising, that is a decision to argue for
    explicitly, not to make by editing a number.
    """
    entries = _entries()

    assert len(entries) <= BASELINE_COUNT, (
        f"the known-failure backlog grew from {BASELINE_COUNT} to {len(entries)}. "
        "A test that newly fails must be fixed, not recorded here."
    )


def test_the_baseline_matches_the_list():
    """Keeps the ratchet constant aligned with the visible backlog."""
    entries = _entries()

    assert len(entries) == BASELINE_COUNT, (
        f"BASELINE_COUNT is {BASELINE_COUNT}, but the backlog has {len(entries)} "
        "entries. Lower the baseline whenever recorded failures are removed."
    )


def test_entries_are_plausible_test_ids():
    for entry in _entries():
        assert entry.startswith("tests/"), f"not a test path: {entry}"
        assert "::" in entry, f"not a specific test id: {entry}"


def test_no_duplicate_entries():
    entries = _entries()

    assert len(entries) == len(set(entries)), "duplicate entries in the backlog"


def test_the_file_explains_itself():
    """Someone hitting this list for the first time needs to know the rule."""
    text = KNOWN_FAILURES.read_text(encoding="utf-8")

    assert "only permitted edit" in text or "never grows" in text
    assert "cost_normal" in text
