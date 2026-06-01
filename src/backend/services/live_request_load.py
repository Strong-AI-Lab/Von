"""Process-global live request-load signal (JVNAUTOSCI-2383).

Tracks the number of in-flight *live* (foreground) conversation-turn requests so
that background subsystems — notably the in-process durable workflow worker —
can yield request-thread and database capacity to the live chat path while a
user is actively waiting for an answer.

This is a support surface only. It carries no task or decision policy; it is a
thread-safe counter plus a small context-manager convenience. Authority for what
the live turn *does* still lives entirely in the workflow/prompt/Vontology
artefacts; this module only answers the operational question "is a user waiting
right now?" so the durable worker can avoid starving the live path with
background evaluation work.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

_LOCK = threading.Lock()
_live_turn_count = 0


def increment_live_turns() -> int:
    """Record that a live foreground turn has started. Returns the new count."""
    global _live_turn_count
    with _LOCK:
        _live_turn_count += 1
        return _live_turn_count


def decrement_live_turns() -> int:
    """Record that a live foreground turn has finished. Returns the new count.

    The counter is clamped at zero so an unbalanced decrement (e.g. a teardown
    firing without a matching increment) can never drive it negative and wedge
    the background-pause logic on indefinitely.
    """
    global _live_turn_count
    with _LOCK:
        if _live_turn_count > 0:
            _live_turn_count -= 1
        return _live_turn_count


def get_live_turn_count() -> int:
    """Return the current number of in-flight live foreground turns."""
    with _LOCK:
        return _live_turn_count


def reset_live_turns() -> None:
    """Reset the counter to zero. Intended for tests only."""
    global _live_turn_count
    with _LOCK:
        _live_turn_count = 0


@contextmanager
def live_turn_in_flight() -> Iterator[None]:
    """Context manager that marks a live foreground turn as in flight."""
    increment_live_turns()
    try:
        yield
    finally:
        decrement_live_turns()
