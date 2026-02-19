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


def test_turn_execution_diagnostics_rebuilds_phase_and_tool_history(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=5000.0)

    von_routes._set_tool_progress(
        "scope-e",
        "req-e",
        {
            "status": "phase_transition",
            "phase": "workflow_discovery",
            "phase_label": "Searching for workflows",
            "request_id": "req-e",
        },
    )
    clock["now"] += 0.2
    von_routes._set_tool_progress(
        "scope-e",
        "req-e",
        {
            "status": "phase_transition",
            "phase": "tool_execute",
            "phase_label": "Executing tools",
            "request_id": "req-e",
        },
    )
    clock["now"] += 0.2
    von_routes._set_tool_progress(
        "scope-e",
        "req-e",
        {
            "status": "tool_call_start",
            "phase": "tool_execute",
            "tool": "search_knowledge_base",
            "batch_size": 2,
            "request_id": "req-e",
        },
    )
    clock["now"] += 0.2
    von_routes._set_tool_progress(
        "scope-e",
        "req-e",
        {
            "status": "tool_invoked",
            "phase": "tool_execute",
            "tool": "search_knowledge_base",
            "batch_size": 2,
            "result_summary": "ok",
            "request_id": "req-e",
        },
    )

    snapshot = von_routes._snapshot_tool_progress_for_request("scope-e", "req-e")
    assert snapshot is not None

    diagnostics = von_routes._build_turn_execution_diagnostics(
        request_id="req-e",
        prompt_text="Summarise indexed notes",
        tool_progress_state=snapshot,
    )

    assert diagnostics["request_id"] == "req-e"
    assert diagnostics["prompt_preview"] == "Summarise indexed notes"
    assert diagnostics["latest_progress"] is not None
    assert diagnostics["progress_events"], "expected reconstructed progress events"

    phase_history = diagnostics["phase_history"]
    assert [entry["phase"] for entry in phase_history] == [
        "workflow_discovery",
        "tool_execute",
    ]

    tool_history = diagnostics["tool_history"]
    assert len(tool_history) == 1
    tool_entry = tool_history[0]
    assert tool_entry["tool"] == "search_knowledge_base"
    assert tool_entry["batchSize"] == 2
    assert tool_entry["resultSummary"] == "ok"
    assert tool_entry["success"] is True

    workflow_stage_model = diagnostics.get("workflow_stage_model")
    assert isinstance(workflow_stage_model, dict)
    assert workflow_stage_model.get("schema_version") == "conversation_turn_stage_model.v1"

    workflow_stage_path = diagnostics.get("workflow_stage_path")
    assert isinstance(workflow_stage_path, dict)
    assert workflow_stage_path.get("schema_version") == "conversation_turn_stage_path.v1"
    path = workflow_stage_path.get("path")
    assert isinstance(path, list)
    assert [entry.get("stage_id") for entry in path] == [
        "workflow_discovery",
        "tool_execute",
    ]
    assert workflow_stage_path.get("has_unmapped_runtime_stages") is False


def test_turn_execution_diagnostics_stage_path_fallback_for_unknown_phase(
    monkeypatch,
) -> None:
    clock = _set_clock(monkeypatch, start=5300.0)

    von_routes._set_tool_progress(
        "scope-e2",
        "req-e2",
        {
            "status": "phase_transition",
            "phase": "new_stage_not_in_catalogue",
            "request_id": "req-e2",
        },
    )
    clock["now"] += 0.1

    snapshot = von_routes._snapshot_tool_progress_for_request("scope-e2", "req-e2")
    assert snapshot is not None

    diagnostics = von_routes._build_turn_execution_diagnostics(
        request_id="req-e2",
        prompt_text="Diagnose a new stage",
        tool_progress_state=snapshot,
    )

    workflow_stage_path = diagnostics.get("workflow_stage_path")
    assert isinstance(workflow_stage_path, dict)
    assert workflow_stage_path.get("has_unmapped_runtime_stages") is True
    assert workflow_stage_path.get("unmapped_runtime_stages") == [
        "new_stage_not_in_catalogue"
    ]
    path = workflow_stage_path.get("path")
    assert isinstance(path, list)
    assert len(path) == 1
    assert path[0].get("mapping_status") == "fallback_unmapped_runtime_stage"
    assert path[0].get("runtime_stage_normalised") == "new_stage_not_in_catalogue"


def test_latest_turn_completion_gate_uses_latest_entry() -> None:
    gate = von_routes._latest_turn_completion_gate(
        [
            {
                "type": "turn_completion_gate",
                "decision": "completed",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
            },
            {"type": "something_else"},
            {
                "type": "turn_completion_gate",
                "decision": "escalation_required",
                "decision_reason": "Required mutation was not executed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_1"],
            },
        ]
    )
    assert gate is not None
    assert gate["decision"] == "escalation_required"
    assert gate["decision_reason"] == "Required mutation was not executed."
    assert gate["safe_to_claim_completion"] is False
    assert gate["requires_follow_up"] is True
    assert gate["blocking_effect_ids"] == ["effect_1"]


def test_terminal_progress_payload_reflects_completion_gate_follow_up() -> None:
    payload = von_routes._build_terminal_tool_progress_payload(
        request_id="req-f",
        aux_calls=[
            {
                "type": "turn_completion_gate",
                "decision": "escalation_required",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_1"],
            }
        ],
    )
    assert payload["request_id"] == "req-f"
    assert payload["status"] == "completed"
    assert payload["success"] is False
    assert payload["completion_gate_decision"] == "escalation_required"
    assert payload["completion_gate_requires_follow_up"] is True
    assert payload["completion_gate_safe_to_claim_completion"] is False
    assert payload["orchestrator_status"] == "follow_up_required"
