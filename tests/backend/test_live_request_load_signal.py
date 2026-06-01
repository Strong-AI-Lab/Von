"""Tests for the process-global live request-load signal (JVNAUTOSCI-2383)."""

from __future__ import annotations

import pytest

from src.backend.services.live_request_load import (
    decrement_live_turns,
    get_live_turn_count,
    increment_live_turns,
    live_turn_in_flight,
    reset_live_turns,
)


@pytest.fixture(autouse=True)
def _reset_counter() -> None:
    reset_live_turns()
    yield
    reset_live_turns()


def test_increment_and_decrement_track_count() -> None:
    assert get_live_turn_count() == 0
    assert increment_live_turns() == 1
    assert increment_live_turns() == 2
    assert get_live_turn_count() == 2
    assert decrement_live_turns() == 1
    assert decrement_live_turns() == 0
    assert get_live_turn_count() == 0


def test_decrement_is_clamped_at_zero() -> None:
    assert decrement_live_turns() == 0
    assert decrement_live_turns() == 0
    assert get_live_turn_count() == 0


def test_context_manager_balances_count() -> None:
    with live_turn_in_flight():
        assert get_live_turn_count() == 1
    assert get_live_turn_count() == 0


def test_context_manager_decrements_on_exception() -> None:
    with pytest.raises(ValueError):
        with live_turn_in_flight():
            assert get_live_turn_count() == 1
            raise ValueError("boom")
    assert get_live_turn_count() == 0
