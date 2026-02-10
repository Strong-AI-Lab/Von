"""Tests for generate() tool-progress liveness telemetry (JVNAUTOSCI-1103)."""

import src.backend.server.routes.von_routes as von_routes


def _set_clock(monkeypatch, start: float = 1000.0):
    clock = {"now": float(start)}
    monkeypatch.setattr(von_routes.time, "time", lambda: clock["now"])
    return clock


def _clear_progress_state() -> None:
    with von_routes._TOOL_PROGRESS_LOCK:
        von_routes._TOOL_PROGRESS.clear()


def setup_function() -> None:
    _clear_progress_state()


def teardown_function() -> None:
    _clear_progress_state()


def test_heartbeat_updates_sequence_and_keeps_stage(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=1000.0)

    von_routes._set_tool_progress(
        "scope-a",
        "req-a",
        {
            "status": "thinking",
            "phase": "tool_execute",
            "phase_label": "Executing tools",
            "request_id": "req-a",
        },
    )
    initial = von_routes._get_tool_progress("scope-a", "req-a")
    assert initial is not None
    assert initial["sequence_no"] == 1
    assert initial["stage"] == "tool_execute"

    clock["now"] += float(von_routes._TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC)
    von_routes._set_tool_progress(
        "scope-a",
        "req-a",
        {
            "status": "heartbeat",
            "request_id": "req-a",
        },
    )
    heartbeat = von_routes._get_tool_progress("scope-a", "req-a")
    assert heartbeat is not None
    assert heartbeat["sequence_no"] == 2
    assert heartbeat["event_kind"] == "heartbeat"
    assert heartbeat["stage"] == "tool_execute"
    assert heartbeat["idle_ms"] >= int(
        von_routes._TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC * 1000
    )


def test_missing_heartbeat_marks_request_stalled(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=2000.0)

    von_routes._set_tool_progress(
        "scope-b",
        "req-b",
        {
            "status": "thinking",
            "phase": "tool_execute",
            "request_id": "req-b",
        },
    )
    stored = von_routes._get_tool_progress("scope-b", "req-b")
    assert stored is not None

    clock["now"] += float(von_routes._TOOL_PROGRESS_STALL_THRESHOLD_SEC + 1)
    serialised = von_routes._serialise_tool_progress_state(
        stored, now_epoch=clock["now"]
    )

    assert serialised["liveness_state"] == "stalled"
    assert serialised["stall_detected"] is True
    assert serialised["liveness_reason"] in {
        "tool_timeout",
        "model_timeout",
        "worker_unavailable",
        "network_silence",
    }


def test_stalled_state_recovers_to_active_after_new_event(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=3000.0)

    von_routes._set_tool_progress(
        "scope-c",
        "req-c",
        {
            "status": "thinking",
            "phase": "tool_execute",
            "request_id": "req-c",
        },
    )
    stalled_source = von_routes._get_tool_progress("scope-c", "req-c")
    assert stalled_source is not None

    clock["now"] += float(von_routes._TOOL_PROGRESS_STALL_THRESHOLD_SEC + 2)
    stalled = von_routes._serialise_tool_progress_state(
        stalled_source, now_epoch=clock["now"]
    )
    assert stalled["liveness_state"] == "stalled"

    clock["now"] += 1.0
    von_routes._set_tool_progress(
        "scope-c",
        "req-c",
        {
            "status": "tool_call_start",
            "tool": "search_knowledge_base",
            "request_id": "req-c",
        },
    )
    recovered_source = von_routes._get_tool_progress("scope-c", "req-c")
    assert recovered_source is not None
    recovered = von_routes._serialise_tool_progress_state(
        recovered_source, now_epoch=clock["now"]
    )

    assert recovered["liveness_state"] == "active"
    assert recovered["sequence_no"] >= 2
    assert recovered["stage"] == "tool_execute"


def test_progress_event_contains_required_telemetry_fields(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=4000.0)

    von_routes._set_tool_progress(
        "scope-d",
        "req-d",
        {
            "status": "llm_call_start",
            "stage": "llm_call",
            "request_id": "req-d",
        },
    )
    clock["now"] += 0.5
    von_routes._set_tool_progress(
        "scope-d",
        "req-d",
        {
            "status": "llm_call_end",
            "stage": "llm_call",
            "request_id": "req-d",
            "duration_ms": 500,
        },
    )

    state = von_routes._get_tool_progress("scope-d", "req-d")
    assert state is not None
    serialised = von_routes._serialise_tool_progress_state(state, now_epoch=clock["now"])

    assert serialised["request_id"] == "req-d"
    assert isinstance(serialised["sequence_no"], int)
    assert serialised["sequence_no"] >= 2
    assert serialised["stage"] == "llm_call"
    assert "activity_idle_ms" in serialised
    assert isinstance(serialised["activity_idle_ms"], int)
