"""Tests for request-scoped progress tracking (JVNAUTOSCI-1038).

Validates that concurrent requests using ProgressTracker maintain isolation.
"""

from typing import Any, Mapping

from src.backend.services.request_progress_service import ProgressTracker


class TestProgressTracker:
    """Unit tests for the ProgressTracker class."""

    def test_emit_calls_callback(self) -> None:
        """emit() should forward info to the callback."""
        captured: list[dict[str, Any]] = []

        def _capture(info: Mapping[str, Any]) -> None:
            captured.append(dict(info))

        tracker = ProgressTracker(callback=_capture)
        tracker.emit({"status": "tool_invoked", "tool": "foo"})

        assert len(captured) == 1
        assert captured[0]["status"] == "tool_invoked"
        assert captured[0]["tool"] == "foo"

    def test_transition_phase_updates_state(self) -> None:
        """transition_phase() should update current_phase and emit."""
        captured: list[dict[str, Any]] = []

        def _capture(info: Mapping[str, Any]) -> None:
            captured.append(dict(info))

        tracker = ProgressTracker(callback=_capture)
        tracker.transition_phase("tool_plan")

        assert tracker.current_phase == "tool_plan"
        assert len(captured) == 1
        assert captured[0]["status"] == "phase_transition"
        assert captured[0]["phase"] == "tool_plan"

    def test_phase_history_accumulates(self) -> None:
        """phase_history should accumulate completed phase transitions.

        Note: The current/active phase is not in history until a new phase starts.
        """
        captured: list[dict[str, Any]] = []

        def _capture(info: Mapping[str, Any]) -> None:
            captured.append(dict(info))

        tracker = ProgressTracker(callback=_capture)
        tracker.transition_phase("tool_plan")
        tracker.transition_phase("tool_execute")
        tracker.transition_phase("completed")

        # History contains previous phases (tool_plan, tool_execute)
        # The "completed" phase is current, not yet in history
        assert len(tracker.phase_history) == 2
        assert [p["phase"] for p in tracker.phase_history] == [
            "tool_plan",
            "tool_execute",
        ]
        # But current_phase tracks the active one
        assert tracker.current_phase == "completed"

    def test_emit_enriches_with_phase_info(self) -> None:
        """emit() should include current phase info if set."""
        captured: list[dict[str, Any]] = []

        def _capture(info: Mapping[str, Any]) -> None:
            captured.append(dict(info))

        tracker = ProgressTracker(callback=_capture)
        tracker.transition_phase("tool_execute")
        tracker.emit({"status": "tool_invoked", "tool": "bar"})

        assert len(captured) == 2
        tool_event = captured[1]
        assert tool_event["status"] == "tool_invoked"
        assert tool_event["phase"] == "tool_execute"
        assert "phase_label" in tool_event

    def test_emit_includes_timing(self) -> None:
        """emit() should include total_elapsed_ms."""
        captured: list[dict[str, Any]] = []

        def _capture(info: Mapping[str, Any]) -> None:
            captured.append(dict(info))

        tracker = ProgressTracker(callback=_capture)
        tracker.emit({"status": "test"})

        assert "total_elapsed_ms" in captured[0]
        assert isinstance(captured[0]["total_elapsed_ms"], int)
        assert captured[0]["total_elapsed_ms"] >= 0

    def test_callback_property_returns_callback(self) -> None:
        """callback property should return the configured callback."""

        def _noop(info: Mapping[str, Any]) -> None:
            pass

        tracker = ProgressTracker(callback=_noop)
        assert tracker.callback is _noop

    def test_none_callback_does_not_raise(self) -> None:
        """emit() and transition_phase() should not raise if callback is None."""
        tracker = ProgressTracker(callback=None)
        tracker.emit({"status": "test"})
        tracker.transition_phase("tool_plan")
        # Should not raise


class TestProgressTrackerIsolation:
    """Tests verifying isolation between concurrent requests."""

    def test_separate_trackers_maintain_isolation(self) -> None:
        """Two trackers should not share state."""
        captured1: list[dict[str, Any]] = []
        captured2: list[dict[str, Any]] = []

        def _capture1(info: Mapping[str, Any]) -> None:
            captured1.append(dict(info))

        def _capture2(info: Mapping[str, Any]) -> None:
            captured2.append(dict(info))

        tracker1 = ProgressTracker(callback=_capture1)
        tracker2 = ProgressTracker(callback=_capture2)

        tracker1.transition_phase("tool_plan")
        tracker2.transition_phase("completed")

        # tracker1 should only have tool_plan
        assert tracker1.current_phase == "tool_plan"
        assert len(captured1) == 1
        assert captured1[0]["phase"] == "tool_plan"

        # tracker2 should only have completed
        assert tracker2.current_phase == "completed"
        assert len(captured2) == 1
        assert captured2[0]["phase"] == "completed"
