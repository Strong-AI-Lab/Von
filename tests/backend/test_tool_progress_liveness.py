"""Tests for generate() tool-progress liveness telemetry (JVNAUTOSCI-1103)."""

import os

from flask import Flask

import src.backend.server.routes.von_routes as von_routes
from src.backend.services.tool_progress_store_service import (
    clear_tool_progress_documents_for_tests,
)


_ORIGINAL_USE_MOCK_DB = os.environ.get("VON_USE_MOCK_DB")
_ORIGINAL_DB_NAME = os.environ.get("VON_DB_NAME")


def _set_clock(monkeypatch, start: float = 1000.0):
    clock = {"now": float(start)}
    monkeypatch.setattr(von_routes.time, "time", lambda: clock["now"])
    return clock


def _clear_progress_state() -> None:
    with von_routes._TOOL_PROGRESS_LOCK:
        von_routes._TOOL_PROGRESS.clear()
    clear_tool_progress_documents_for_tests()


def setup_function() -> None:
    os.environ["VON_USE_MOCK_DB"] = "1"
    os.environ["VON_DB_NAME"] = "test_von_db"
    _clear_progress_state()


def teardown_function() -> None:
    _clear_progress_state()
    if _ORIGINAL_USE_MOCK_DB is None:
        os.environ.pop("VON_USE_MOCK_DB", None)
    else:
        os.environ["VON_USE_MOCK_DB"] = _ORIGINAL_USE_MOCK_DB
    if _ORIGINAL_DB_NAME is None:
        os.environ.pop("VON_DB_NAME", None)
    else:
        os.environ["VON_DB_NAME"] = _ORIGINAL_DB_NAME


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


def test_progress_endpoint_reads_persisted_state_after_local_cache_miss(
    monkeypatch,
) -> None:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: True,
    )

    scope_key = "user:#V#test_user"
    von_routes._set_tool_progress(
        scope_key,
        "req-persisted",
        {
            "status": "thinking",
            "phase": "workflow_dispatch",
            "phase_label": "Selecting workflow",
            "request_id": "req-persisted",
            "goal_label": "https://arxiv.org/abs/2602.20478",
        },
    )

    with von_routes._TOOL_PROGRESS_LOCK:
        von_routes._TOOL_PROGRESS.clear()

    client = app.test_client()
    response = client.get("/von/progress/req-persisted")
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("request_id") == "req-persisted"
    assert body.get("stage") == "workflow_dispatch"
    assert body.get("phase_label") == "Selecting workflow"
    assert body.get("goal_label") == "https://arxiv.org/abs/2602.20478"


def test_progress_endpoint_pending_response_includes_explanatory_payload(
    monkeypatch,
) -> None:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: True,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: None,
    )

    client = app.test_client()
    response = client.get("/von/progress/req-pending")
    assert response.status_code == 202

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("status") == "pending"
    assert body.get("phase") == "context_build"
    assert body.get("phase_label") == "Awaiting visible progress"
    assert body.get("pending_reason") == "no_visible_progress_state"
    assert "No live progress state is visible yet" in str(body.get("result_summary"))


def test_response_finalising_payload_includes_useful_detail() -> None:
    payload = von_routes._build_response_finalising_tool_progress_payload(
        request_id="req-finalise",
        eta_ms=1800,
        response_text="Final answer",
        tool_message_count=2,
        persist_history=True,
    )

    assert payload["phase"] == "response_finalising"
    assert payload["phase_label"] == "Finalising response"
    assert payload["subtask"] == "response assembly"
    assert "assembling the final response payload" in payload["result_summary"]
    assert "summarising 2 tool results" in payload["result_summary"]
    assert "persisting chat history" in payload["result_summary"]


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


def test_progress_goal_label_and_candidate_count_are_serialised(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=4100.0)

    von_routes._set_tool_progress(
        "scope-goal",
        "req-goal",
        {
            "status": "thinking",
            "phase": "workflow_discovery_complete",
            "request_id": "req-goal",
            "goal_label": "Fully represent the paper #V#uploaded_file_copy_123",
            "workflow_match_count": 0,
            "workflow_candidate_count": 1,
        },
    )

    state = von_routes._get_tool_progress("scope-goal", "req-goal")
    assert state is not None

    serialised = von_routes._serialise_tool_progress_state(state, now_epoch=clock["now"])
    assert (
        serialised["goal_label"]
        == "Fully represent the paper #V#uploaded_file_copy_123"
    )

    events = serialised.get("diagnostic_events")
    assert isinstance(events, list)
    assert events[-1].get("goal_label") == serialised["goal_label"]
    assert events[-1].get("workflow_candidate_count") == 1

    progress_events = von_routes._normalise_progress_events_from_diagnostic_events(events)
    assert progress_events[-1].get("goal_label") == serialised["goal_label"]

    summary = von_routes._build_tool_progress_compact_summary(serialised)
    assert isinstance(summary, dict)
    assert summary.get("goal_label") == serialised["goal_label"]


def test_live_progress_serialisation_includes_workflow_stage_path(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=4200.0)

    von_routes._set_tool_progress(
        "scope-workflow",
        "req-workflow",
        {
            "status": "thinking",
            "phase": "workflow_discovery",
            "request_id": "req-workflow",
        },
    )
    clock["now"] += 0.1
    von_routes._set_tool_progress(
        "scope-workflow",
        "req-workflow",
        {
            "status": "phase_transition",
            "phase": "workflow_dispatch",
            "request_id": "req-workflow",
            "selected_workflow_id": "#V#tool_calling_workflow",
            "selected_workflow_name": "Tool calling workflow",
        },
    )

    state = von_routes._get_tool_progress("scope-workflow", "req-workflow")
    assert state is not None

    serialised = von_routes._serialise_tool_progress_state(
        state,
        now_epoch=clock["now"],
    )

    workflow_stage_path = serialised.get("workflow_stage_path")
    assert isinstance(workflow_stage_path, dict)
    assert workflow_stage_path.get("workflow_id") == "#V#tool_calling_workflow"
    path = workflow_stage_path.get("path")
    assert isinstance(path, list)
    assert [entry.get("stage_id") for entry in path] == [
        "workflow_discovery",
        "workflow_dispatch",
    ]


def test_workflow_discovery_progress_payload_preserves_explicit_no_match_state() -> None:
    payload = von_routes._normalise_workflow_discovery_progress_payload(
        None,
        query="find a workflow for this task",
        namespace="#V#test_user",
    )

    assert payload["query"] == "find a workflow for this task"
    assert payload["requested_query"] == "find a workflow for this task"
    assert payload["namespace"] == "#V#test_user"
    assert payload["matches"] == []
    assert payload["routing_matches"] == []
    assert payload["candidates"] == []
    assert payload["match_count"] == 0
    assert payload["candidate_count"] == 0
    assert payload["errors"] is None


def test_orchestrator_start_uses_startup_wait_liveness_reason() -> None:
    assert (
        von_routes._classify_progress_cause("orchestrator_start", "heartbeat")
        == "orchestrator_startup_wait"
    )
    assert (
        von_routes._classify_progress_cause("tool_plan", "worker_unavailable")
        == "worker_unavailable"
    )


def test_progress_clears_stale_error_on_subsequent_success(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=4500.0)

    von_routes._set_tool_progress(
        "scope-stale",
        "req-stale",
        {
            "status": "llm_call_end",
            "stage": "buttonify",
            "request_id": "req-stale",
            "model": "granite3.3:2b",
            "success": False,
            "error": "Ollama unexpected error: failed to connect",
            "error_class": "RuntimeError",
            "failure_kind": "provider_unreachable",
        },
    )
    clock["now"] += 0.2
    von_routes._set_tool_progress(
        "scope-stale",
        "req-stale",
        {
            "status": "llm_call_end",
            "stage": "buttonify",
            "request_id": "req-stale",
            "model": "gpt-5.2-chat-latest",
            "success": True,
        },
    )

    state = von_routes._get_tool_progress("scope-stale", "req-stale")
    assert state is not None
    assert "error" not in state
    assert "error_class" not in state
    assert "failure_kind" not in state

    events = state.get("diagnostic_events")
    assert isinstance(events, list)
    assert events[0].get("error") == "Ollama unexpected error: failed to connect"
    assert events[0].get("error_class") == "RuntimeError"
    assert events[0].get("failure_kind") == "provider_unreachable"
    assert "error" not in events[-1]
    assert "error_class" not in events[-1]
    assert "failure_kind" not in events[-1]


def test_progress_clears_stale_success_on_error(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=4700.0)

    von_routes._set_tool_progress(
        "scope-stale-success",
        "req-stale-success",
        {
            "status": "llm_call_end",
            "stage": "tool_plan",
            "request_id": "req-stale-success",
            "success": True,
        },
    )
    clock["now"] += 0.1
    von_routes._set_tool_progress(
        "scope-stale-success",
        "req-stale-success",
        {
            "status": "error",
            "stage": "tool_call",
            "request_id": "req-stale-success",
            "error": "list assignment index out of range",
        },
    )

    state = von_routes._get_tool_progress("scope-stale-success", "req-stale-success")
    assert state is not None
    assert state["status"] == "error"
    assert state.get("success") is not True
    assert state.get("error") == "list assignment index out of range"

    events = state.get("diagnostic_events")
    assert isinstance(events, list)
    assert events[-1].get("status") == "error"
    assert events[-1].get("success") is None


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
        llm_calls=[
            {
                "type": "llm.generate_with_tools",
                "stage": "tool_execute",
                "model": "test-model",
                "duration_ms": 125.8,
                "provider": "openai",
            }
        ],
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

    stage_diagnostics = diagnostics.get("stage_diagnostics")
    assert isinstance(stage_diagnostics, list)
    assert [entry.get("stage_id") for entry in stage_diagnostics] == [
        "workflow_discovery",
        "tool_execute",
    ]
    tool_stage_diagnostics = stage_diagnostics[-1]
    assert tool_stage_diagnostics.get("tool_history")
    assert tool_stage_diagnostics.get("event_count") == 3

    timing = diagnostics.get("timing_breakdown")
    assert isinstance(timing, dict)
    assert timing.get("schema_version") == "conversation_turn_timing_breakdown.v1"
    stage_rows = timing.get("stages")
    assert isinstance(stage_rows, list)
    stage_by_name = {
        entry.get("stage"): entry
        for entry in stage_rows
        if isinstance(entry, dict) and isinstance(entry.get("stage"), str)
    }
    tool_stage = stage_by_name.get("tool_execute")
    assert isinstance(tool_stage, dict)
    assert tool_stage.get("llm_elapsed_ms") == 125
    assert tool_stage.get("llm_call_count") == 1

    llm_rows = timing.get("llm_calls_by_stage_model")
    assert isinstance(llm_rows, list)
    assert llm_rows
    first_llm = llm_rows[0]
    assert first_llm.get("stage") == "tool_execute"
    assert first_llm.get("model") == "test-model"
    assert first_llm.get("duration_ms") == 125


def test_canonical_tool_summary_survives_long_heartbeat_tail(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=5200.0)

    von_routes._set_tool_progress(
        "scope-heartbeat-tail",
        "req-heartbeat-tail",
        {
            "status": "phase_transition",
            "phase": "tool_execute",
            "phase_label": "Executing tools",
            "request_id": "req-heartbeat-tail",
        },
    )
    clock["now"] += 0.1
    von_routes._set_tool_progress(
        "scope-heartbeat-tail",
        "req-heartbeat-tail",
        {
            "status": "tool_call_start",
            "phase": "tool_execute",
            "tool": "download_paper",
            "batch_size": 1,
            "call_id": "call-download-paper",
            "request_id": "req-heartbeat-tail",
        },
    )
    clock["now"] += 0.1
    von_routes._set_tool_progress(
        "scope-heartbeat-tail",
        "req-heartbeat-tail",
        {
            "status": "tool_invoked",
            "phase": "tool_execute",
            "tool": "download_paper",
            "batch_size": 1,
            "call_id": "call-download-paper",
            "result_summary": "Downloaded: 2510.06018",
            "request_id": "req-heartbeat-tail",
        },
    )

    for _ in range(von_routes._TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT + 5):
        clock["now"] += float(von_routes._TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC)
        von_routes._set_tool_progress(
            "scope-heartbeat-tail",
            "req-heartbeat-tail",
            {
                "status": "heartbeat",
                "request_id": "req-heartbeat-tail",
            },
        )

    snapshot = von_routes._snapshot_tool_progress_for_request(
        "scope-heartbeat-tail",
        "req-heartbeat-tail",
    )
    assert snapshot is not None

    diagnostic_events = snapshot.get("diagnostic_events")
    assert isinstance(diagnostic_events, list)
    assert len(diagnostic_events) == von_routes._TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT
    assert all(event.get("status") != "tool_invoked" for event in diagnostic_events)

    assert snapshot.get("tool_call_count") == 1
    assert snapshot.get("tool_success_count") == 1
    assert snapshot.get("tool_failure_count") == 0
    assert snapshot.get("tool_pending_count") == 0
    tool_history = snapshot.get("tool_history")
    assert isinstance(tool_history, list)
    assert tool_history == [
        {
            "tool": "download_paper",
            "workflowTask": "",
            "batchSize": 1,
            "phase": "tool_execute",
            "resultSummary": "Downloaded: 2510.06018",
            "success": True,
            "callId": "call-download-paper",
        }
    ]

    diagnostics = von_routes._build_turn_execution_diagnostics(
        request_id="req-heartbeat-tail",
        prompt_text="https://arxiv.org/abs/2510.06018 represent that paper",
        tool_progress_state=snapshot,
    )
    assert diagnostics["tool_call_count"] == 1
    assert diagnostics["tool_success_count"] == 1
    assert diagnostics["tool_failure_count"] == 0
    assert diagnostics["tool_pending_count"] == 0
    assert diagnostics["tool_history"] == tool_history

    stage_diagnostics = diagnostics.get("stage_diagnostics")
    assert isinstance(stage_diagnostics, list)
    assert len(stage_diagnostics) == 1
    assert stage_diagnostics[0] == {
        **stage_diagnostics[0],
        "stage_id": "tool_execute",
        "stage_label": "Execute tool calls",
        "event_count": von_routes._TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT,
        "latest_status": "heartbeat",
        "latest_at_utc": snapshot.get("updated_at"),
        "latest_result_summary": "Downloaded: 2510.06018",
        "latest_error": None,
        "tool_call_count": 1,
        "tool_success_count": 1,
        "tool_failure_count": 0,
        "tool_pending_count": 0,
        "tool_call_start_count": 1,
        "tool_call_end_count": 1,
        "tool_history": tool_history,
    }


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
    stage_diagnostics = diagnostics.get("stage_diagnostics")
    assert isinstance(stage_diagnostics, list)
    assert stage_diagnostics[0].get("stage_id") == "new_stage_not_in_catalogue"


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
    assert payload["status"] == "follow_up_required"
    assert payload["stage"] == "follow_up_required"
    assert payload["phase_label"] == "Follow-up required"
    assert payload["success"] is False
    assert payload["completion_gate_decision"] == "escalation_required"
    assert payload["completion_gate_requires_follow_up"] is True
    assert payload["completion_gate_safe_to_claim_completion"] is False
    assert payload["orchestrator_status"] == "follow_up_required"


def test_response_finalising_payload_is_non_terminal_with_eta() -> None:
    payload = von_routes._build_response_finalising_tool_progress_payload(
        request_id="req-finalise",
        eta_ms=1750,
        response_text="Final response",
        tool_message_count=0,
        persist_history=False,
    )
    assert payload["request_id"] == "req-finalise"
    assert payload["status"] == "phase_transition"
    assert payload["stage"] == "response_finalising"
    assert payload["phase"] == "response_finalising"
    assert payload["phase_label"] == "Finalising response"
    assert payload["workflow_task"] == "response_finalising"
    assert payload["subtask"] == "response assembly"
    assert "assembling the final response payload" in payload["result_summary"]
    assert payload["eta_ms"] == 1750


def test_response_finalising_eta_estimate_is_bounded() -> None:
    baseline = von_routes._estimate_response_finalising_eta_ms(
        response_text=None,
        tool_message_count=0,
        persist_history=False,
    )
    assert 800 <= baseline <= 12_000

    elevated = von_routes._estimate_response_finalising_eta_ms(
        response_text="x" * 20_000,
        tool_message_count=400,
        persist_history=True,
    )
    assert 800 <= elevated <= 12_000
    assert elevated >= baseline


def test_default_stage_label_includes_response_finalising() -> None:
    assert von_routes._default_stage_label("response_finalising") == "Finalising response"
