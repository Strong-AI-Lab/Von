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


def test_get_tool_progress_prefers_live_memory_state_before_persisted_fetch(
    monkeypatch,
) -> None:
    von_routes._set_tool_progress(
        "scope-live",
        "req-live",
        {
            "status": "thinking",
            "phase": "tool_execute",
            "phase_label": "Executing tools",
            "request_id": "req-live",
            "subtask": "workflow execution",
        },
    )

    def _unexpected_fetch(*, scope_key: str, request_id: str):
        raise AssertionError(
            "persisted progress fetch should not run when live memory state exists"
        )

    monkeypatch.setattr(von_routes, "fetch_tool_progress_state", _unexpected_fetch)

    state = von_routes._get_tool_progress("scope-live", "req-live")

    assert state is not None
    assert state["request_id"] == "req-live"
    assert state["phase"] == "tool_execute"
    assert state["subtask"] == "workflow execution"


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


def test_progress_endpoint_uses_window_session_scope_for_anonymous_requests(
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

    window_session_id = "ws_progress_visibility_race"
    with app.test_request_context(
        "/von/generate",
        headers={"X-Von-Window-Session": window_session_id},
    ):
        scope_key = von_routes._get_tool_progress_scope_key()

    assert scope_key == f"anon:window:{window_session_id}"

    von_routes._set_tool_progress(
        scope_key,
        "req-window-scope",
        {
            "status": "thinking",
            "phase": "workflow_discovery",
            "phase_label": "Searching for workflows",
            "request_id": "req-window-scope",
            "goal_label": "https://arxiv.org/abs/2602.20478",
        },
    )

    with von_routes._TOOL_PROGRESS_LOCK:
        von_routes._TOOL_PROGRESS.clear()

    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["tool_progress_scope"] = "legacy_cookie_scope"

    response = client.get(
        "/von/progress/req-window-scope",
        headers={"X-Von-Window-Session": window_session_id},
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("request_id") == "req-window-scope"
    assert body.get("stage") == "workflow_discovery"
    assert body.get("phase_label") == "Searching for workflows"
    assert body.get("goal_label") == "https://arxiv.org/abs/2602.20478"


def test_progress_endpoint_retains_legacy_session_scope_without_window_header(
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
    with client.session_transaction() as flask_session:
        flask_session["tool_progress_scope"] = "legacy_cookie_scope"

    with app.test_request_context("/von/generate"):
        from flask import session as request_session

        request_session["tool_progress_scope"] = "legacy_cookie_scope"
        scope_key = von_routes._get_tool_progress_scope_key()

    assert scope_key == "anon:session:legacy_cookie_scope"

    von_routes._set_tool_progress(
        scope_key,
        "req-legacy-scope",
        {
            "status": "thinking",
            "phase": "context_build",
            "phase_label": "Building context",
            "request_id": "req-legacy-scope",
        },
    )

    with von_routes._TOOL_PROGRESS_LOCK:
        von_routes._TOOL_PROGRESS.clear()

    response = client.get("/von/progress/req-legacy-scope")
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("request_id") == "req-legacy-scope"
    assert body.get("stage") == "context_build"
    assert body.get("phase_label") == "Building context"


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


def test_live_stage_diagnostics_surface_prepared_llm_request_before_aux_persistence(
    monkeypatch,
) -> None:
    clock = _set_clock(monkeypatch, start=4050.0)

    request_payload = {
        "prompt": {
            "text": "Answer using the authenticated current user context.",
            "char_count": 49,
        },
        "context_summary": {
            "message_count": 3,
            "leading_system_message_count": 1,
            "role_counts": {"system": 1, "user": 1, "assistant": 1},
            "total_content_chars": 128,
        },
        "context_message_count": 3,
    }
    prepared_at = "2026-04-15T01:02:03Z"
    sent_at = "2026-04-15T01:02:04Z"

    von_routes._set_tool_progress(
        "scope-live-llm",
        "req-live-llm",
        {
            "status": "llm_request_prepared",
            "stage": "workflow_dispatch_prepare",
            "phase": "workflow_dispatch_prepare",
            "request_id": "req-live-llm",
            "llm_request": request_payload,
            "llm_request_state": "prepared",
            "llm_request_prepared_at_utc": prepared_at,
        },
    )
    clock["now"] += 0.25
    von_routes._set_tool_progress(
        "scope-live-llm",
        "req-live-llm",
        {
            "status": "llm_call_start",
            "stage": "workflow_dispatch_prepare",
            "phase": "workflow_dispatch_prepare",
            "request_id": "req-live-llm",
            "model": "gemma4:26b",
            "provider": "ollama",
            "llm_request": request_payload,
            "llm_request_state": "sent",
            "llm_request_prepared_at_utc": prepared_at,
            "llm_request_sent_at_utc": sent_at,
            "fallback_attempt_no": 1,
            "fallback_candidate_count": 3,
        },
    )

    state = von_routes._get_tool_progress("scope-live-llm", "req-live-llm")
    assert state is not None
    serialised = von_routes._serialise_tool_progress_state(state, now_epoch=clock["now"])

    stage_diagnostics = serialised.get("stage_diagnostics")
    assert isinstance(stage_diagnostics, list)
    workflow_dispatch_prepare = next(
        (
            entry
            for entry in stage_diagnostics
            if isinstance(entry, dict)
            and entry.get("stage_id") == "workflow_dispatch_prepare"
        ),
        None,
    )
    assert workflow_dispatch_prepare is not None
    assert workflow_dispatch_prepare.get("llm_input_recorded") is True
    assert workflow_dispatch_prepare.get("llm_output_recorded") is False
    assert workflow_dispatch_prepare.get("llm_exchange_record_count") == 1
    assert workflow_dispatch_prepare.get("llm_exchange_entry_types") == [
        "live_llm_request"
    ]
    assert workflow_dispatch_prepare.get("latest_llm_exchange") == {
        "entry_type": "live_llm_request",
        "stage": "workflow_dispatch_prepare",
        "llm_input_recorded": True,
        "llm_output_recorded": False,
        "prompt_preview": request_payload["prompt"],
        "context_summary": request_payload["context_summary"],
        "context_message_count": 3,
        "selected_model": "gemma4:26b",
        "selected_provider": "ollama",
        "fallback_attempt_no": 1,
        "fallback_candidate_count": 3,
        "llm_request_state": "sent",
        "llm_request_prepared_at_utc": prepared_at,
        "llm_request_sent_at_utc": sent_at,
    }

    diagnostic_events = serialised.get("diagnostic_events")
    assert isinstance(diagnostic_events, list)
    prepared_event = next(
        (
            entry
            for entry in diagnostic_events
            if isinstance(entry, dict)
            and entry.get("status") == "llm_request_prepared"
        ),
        None,
    )
    assert prepared_event is not None
    assert prepared_event.get("llm_request", {}).get("prompt", {}).get("text") == (
        request_payload["prompt"]["text"]
    )


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
            "status": "orchestrator_start",
            "stage": "workflow_dispatch_prepare",
            "phase": "workflow_dispatch_prepare",
            "request_id": "req-workflow",
            "result_summary": "Preparing workflow dispatch",
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
        "workflow_dispatch_prepare",
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


def test_pre_dispatch_prepare_uses_startup_wait_liveness_reason() -> None:
    assert (
        von_routes._classify_progress_cause("workflow_dispatch_prepare", "heartbeat")
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
    assert len(path) == 2
    assert path[0].get("stage_id") == "workflow_discovery"
    assert path[1].get("runtime_stage_normalised") == "tool_execute"
    assert path[1].get("stage_id") in {"tool_execute", None}

    stage_diagnostics = diagnostics.get("stage_diagnostics")
    assert isinstance(stage_diagnostics, list)
    assert stage_diagnostics[0].get("stage_id") == "workflow_discovery"
    assert stage_diagnostics[-1].get("stage_id") in {"tool_execute", None}
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
        "stage_label": stage_diagnostics[0].get("stage_label"),
        "event_count": von_routes._TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT + 8,
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


def test_live_stage_path_and_stage_diagnostics_survive_long_finalising_heartbeat_tail(
    monkeypatch,
) -> None:
    clock = _set_clock(monkeypatch, start=5250.0)

    von_routes._set_tool_progress(
        "scope-stage-tail",
        "req-stage-tail",
        {
            "status": "thinking",
            "phase": "context_build",
            "phase_label": "Building context",
            "result_summary": "Resolving session scope and chat history.",
            "request_id": "req-stage-tail",
        },
    )
    clock["now"] += 0.1
    von_routes._set_tool_progress(
        "scope-stage-tail",
        "req-stage-tail",
        {
            "status": "thinking",
            "phase": "workflow_discovery",
            "phase_label": "Searching for workflows",
            "result_summary": "Searching applicable workflows for the turn.",
            "request_id": "req-stage-tail",
        },
    )
    clock["now"] += 0.1
    von_routes._set_tool_progress(
        "scope-stage-tail",
        "req-stage-tail",
        {
            "status": "phase_transition",
            "phase": "response_finalising",
            "phase_label": "Finalising response",
            "result_summary": "Assembling the final response payload.",
            "request_id": "req-stage-tail",
        },
    )

    for _ in range(von_routes._TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT + 10):
        clock["now"] += float(von_routes._TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC)
        von_routes._set_tool_progress(
            "scope-stage-tail",
            "req-stage-tail",
            {
                "status": "heartbeat",
                "request_id": "req-stage-tail",
            },
        )

    snapshot = von_routes._snapshot_tool_progress_for_request(
        "scope-stage-tail",
        "req-stage-tail",
    )
    assert snapshot is not None

    workflow_stage_path = snapshot.get("workflow_stage_path")
    assert isinstance(workflow_stage_path, dict)
    path = workflow_stage_path.get("path")
    assert isinstance(path, list)
    assert [entry.get("stage_id") for entry in path] == [
        "context_build",
        "workflow_discovery",
        "response_finalising",
    ]

    stage_diagnostics = snapshot.get("stage_diagnostics")
    assert isinstance(stage_diagnostics, list)
    assert [entry.get("stage_id") for entry in stage_diagnostics] == [
        "context_build",
        "workflow_discovery",
        "response_finalising",
    ]
    assert stage_diagnostics[0].get("event_count") == 1
    assert stage_diagnostics[0].get("latest_result_summary") == (
        "Resolving session scope and chat history."
    )
    assert stage_diagnostics[1].get("event_count") == 1
    assert stage_diagnostics[1].get("latest_result_summary") == (
        "Searching applicable workflows for the turn."
    )
    assert stage_diagnostics[2].get("event_count") == (
        von_routes._TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT + 11
    )
    phase_history = snapshot.get("phase_history")
    assert isinstance(phase_history, list)
    assert [entry.get("phase") for entry in phase_history] == [
        "context_build",
        "workflow_discovery",
        "response_finalising",
    ]
    progress_events = snapshot.get("progress_events")
    assert isinstance(progress_events, list)
    assert [entry.get("stage") for entry in progress_events] == [
        "context_build",
        "workflow_discovery",
        "response_finalising",
    ]
    activity_history = snapshot.get("activity_history")
    assert isinstance(activity_history, list)
    assert [entry.get("stage") for entry in activity_history] == [
        "context_build",
        "workflow_discovery",
        "response_finalising",
    ]
    assert activity_history[-1].get("state") == "pending"

    diagnostics = von_routes._build_turn_execution_diagnostics(
        request_id="req-stage-tail",
        prompt_text="https://arxiv.org/abs/2602.20478",
        tool_progress_state=snapshot,
    )
    diagnostics_stage_path = diagnostics.get("workflow_stage_path")
    assert isinstance(diagnostics_stage_path, dict)
    diagnostics_path = diagnostics_stage_path.get("path")
    assert isinstance(diagnostics_path, list)
    assert [entry.get("stage_id") for entry in diagnostics_path] == [
        "context_build",
        "workflow_discovery",
        "response_finalising",
    ]
    diagnostics_stage_diagnostics = diagnostics.get("stage_diagnostics")
    assert isinstance(diagnostics_stage_diagnostics, list)
    assert [entry.get("stage_id") for entry in diagnostics_stage_diagnostics] == [
        "context_build",
        "workflow_discovery",
        "response_finalising",
    ]
    diagnostics_phase_history = diagnostics.get("phase_history")
    assert isinstance(diagnostics_phase_history, list)
    assert [entry.get("phase") for entry in diagnostics_phase_history] == [
        "context_build",
        "workflow_discovery",
        "response_finalising",
    ]
    diagnostics_progress_events = diagnostics.get("progress_events")
    assert isinstance(diagnostics_progress_events, list)
    assert [entry.get("stage") for entry in diagnostics_progress_events] == [
        "context_build",
        "workflow_discovery",
        "response_finalising",
    ]
    diagnostics_activity_history = diagnostics.get("activity_history")
    assert isinstance(diagnostics_activity_history, list)
    assert [entry.get("stage") for entry in diagnostics_activity_history] == [
        "context_build",
        "workflow_discovery",
        "response_finalising",
    ]
    diagnostics_timing = diagnostics.get("timing_breakdown")
    assert isinstance(diagnostics_timing, dict)
    diagnostics_stage_rows = diagnostics_timing.get("stages")
    assert isinstance(diagnostics_stage_rows, list)
    diagnostics_stage_names = [
        entry.get("stage")
        for entry in diagnostics_stage_rows
        if isinstance(entry, dict)
    ]
    assert diagnostics_stage_names[:3] == [
        "context_build",
        "workflow_discovery",
        "response_finalising",
    ]

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


def test_non_terminal_progress_update_clears_stale_error_code(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=5400.0)

    von_routes._set_tool_progress(
        "scope-stale-error",
        "req-stale-error",
        {
            "status": "error",
            "phase": "tool_execute",
            "phase_label": "Executing tools",
            "request_id": "req-stale-error",
            "error": "Task not found",
            "error_code": "NOT_FOUND",
        },
    )
    clock["now"] += 0.1
    von_routes._set_tool_progress(
        "scope-stale-error",
        "req-stale-error",
        {
            "status": "phase_transition",
            "phase": "response_finalising",
            "phase_label": "Finalising response",
            "request_id": "req-stale-error",
            "result_summary": "Assembling the final response payload.",
        },
    )

    snapshot = von_routes._snapshot_tool_progress_for_request(
        "scope-stale-error",
        "req-stale-error",
    )
    assert snapshot is not None
    assert snapshot.get("stage") == "response_finalising"
    assert "error" not in snapshot
    assert "error_code" not in snapshot


def test_stage_diagnostics_include_workflow_selection_rationale(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=5450.0)

    von_routes._set_tool_progress(
        "scope-routing-rationale",
        "req-routing-rationale",
        {
            "status": "phase_transition",
            "phase": "workflow_dispatch",
            "phase_label": "Selecting workflow",
            "request_id": "req-routing-rationale",
            "selected_workflow_id": "#V#chat_assistant_workflow",
            "selected_workflow_name": "Chat assistant workflow",
            "workflow_selector_verdict": "rag_selected",
            "workflow_selector_source": "selector",
            "workflow_selection_rationale": (
                "fallback_selected:#V#chat_assistant_workflow:zero_discovered_matches"
            ),
        },
    )
    clock["now"] += 0.1

    snapshot = von_routes._snapshot_tool_progress_for_request(
        "scope-routing-rationale",
        "req-routing-rationale",
    )
    assert snapshot is not None

    diagnostics = von_routes._build_turn_execution_diagnostics(
        request_id="req-routing-rationale",
        prompt_text="Do you think you can make the workflow for adding academic talks now?",
        tool_progress_state=snapshot,
    )

    stage_diagnostics = diagnostics.get("stage_diagnostics")
    assert isinstance(stage_diagnostics, list)
    workflow_dispatch = next(
        (
            entry
            for entry in stage_diagnostics
            if isinstance(entry, dict) and entry.get("stage_id") == "workflow_dispatch"
        ),
        None,
    )
    assert workflow_dispatch is not None
    assert workflow_dispatch.get("selected_workflow_id") == "#V#chat_assistant_workflow"
    assert workflow_dispatch.get("workflow_selector_verdict") == "rag_selected"
    assert workflow_dispatch.get("workflow_selection_rationale") == (
        "fallback_selected:#V#chat_assistant_workflow:zero_discovered_matches"
    )


def test_stage_diagnostics_backfill_selected_workflow_from_routing_diagnostics(
    monkeypatch,
) -> None:
    clock = _set_clock(monkeypatch, start=5480.0)

    selected_workflow_id = "#V#arxiv_paper_representation_workflow"
    workflow_name = "Arxiv Paper Representation Workflow"

    von_routes._set_tool_progress(
        "scope-routing-backfill",
        "req-routing-backfill",
        {
            "status": "thinking",
            "phase": "workflow_dispatch_prepare",
            "phase_label": "Workflow dispatch preparation",
            "request_id": "req-routing-backfill",
            "result_summary": "Preparing workflow dispatch",
            "workflow_discovery": {
                "candidate_count": 2,
                "match_count": 1,
                "candidates": [
                    {
                        "concept_id": selected_workflow_id,
                        "name": workflow_name,
                        "routing_eligible": True,
                    },
                    {
                        "concept_id": "#V#arxiv_paper_ingestion_testing_workflow",
                        "name": "Arxiv Paper Ingestion Testing Workflow",
                        "routing_eligible": True,
                    },
                ],
                "matches": [
                    {
                        "concept_id": selected_workflow_id,
                        "name": workflow_name,
                        "routing_eligible": True,
                    }
                ],
            },
            "workflow_routing": {
                "workflow_id": selected_workflow_id,
                "verdict": "rag_selected",
                "source": "selector",
                "selection_rationale": "selector_selected_discovered_candidate",
            },
            "workflow_routing_aux": [
                {
                    "type": "workflow_selector_prompt",
                    "prompt_id": "#V#chat_turn_classifier_prompt",
                    "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
                    "candidate_list": {
                        "text": "- #V#arxiv_paper_representation_workflow",
                        "char_count": 38,
                    },
                },
                {
                    "type": "workflow_selector",
                    "workflow_id": selected_workflow_id,
                    "verdict": "rag_selected",
                    "selection_source": "selector",
                    "response": {
                        "text": selected_workflow_id,
                        "char_count": len(selected_workflow_id),
                    },
                },
            ],
            "counters": {"tools_started": 0, "tools_completed": 0},
        },
    )
    clock["now"] += 0.1
    von_routes._set_tool_progress(
        "scope-routing-backfill",
        "req-routing-backfill",
        {
            "status": "phase_transition",
            "phase": "response_finalising",
            "phase_label": "Finalising response",
            "request_id": "req-routing-backfill",
            "result_summary": "assembling the final response payload",
        },
    )

    snapshot = von_routes._snapshot_tool_progress_for_request(
        "scope-routing-backfill",
        "req-routing-backfill",
    )
    assert snapshot is not None
    assert snapshot.get("selected_workflow_id") == selected_workflow_id
    assert snapshot.get("selected_workflow_name") == workflow_name
    assert snapshot.get("workflow_selector_verdict") == "rag_selected"
    assert snapshot.get("workflow_selector_source") == "selector"
    assert snapshot.get("workflow_selection_rationale") == (
        "selector_selected_discovered_candidate"
    )

    stage_diagnostics = snapshot.get("stage_diagnostics")
    assert isinstance(stage_diagnostics, list)
    workflow_dispatch_prepare = next(
        (
            entry
            for entry in stage_diagnostics
            if isinstance(entry, dict)
            and entry.get("stage_id") == "workflow_dispatch_prepare"
        ),
        None,
    )
    assert workflow_dispatch_prepare is not None
    assert workflow_dispatch_prepare.get("selected_workflow_id") == selected_workflow_id
    assert workflow_dispatch_prepare.get("selected_workflow_name") == workflow_name
    assert workflow_dispatch_prepare.get("workflow_selector_verdict") == "rag_selected"
    assert workflow_dispatch_prepare.get("workflow_selector_source") == "selector"
    assert workflow_dispatch_prepare.get("workflow_selection_rationale") == (
        "selector_selected_discovered_candidate"
    )

    diagnostics = von_routes._build_turn_execution_diagnostics(
        request_id="req-routing-backfill",
        prompt_text="https://arxiv.org/abs/2502.13025",
        tool_progress_state=snapshot,
    )
    rebuilt_stage_diagnostics = diagnostics.get("stage_diagnostics")
    assert isinstance(rebuilt_stage_diagnostics, list)
    rebuilt_dispatch_prepare = next(
        (
            entry
            for entry in rebuilt_stage_diagnostics
            if isinstance(entry, dict)
            and entry.get("stage_id") == "workflow_dispatch_prepare"
        ),
        None,
    )
    assert rebuilt_dispatch_prepare is not None
    assert rebuilt_dispatch_prepare.get("selected_workflow_id") == selected_workflow_id
    assert rebuilt_dispatch_prepare.get("selected_workflow_name") == workflow_name
    assert rebuilt_dispatch_prepare.get("workflow_selector_verdict") == "rag_selected"
    assert rebuilt_dispatch_prepare.get("workflow_selector_source") == "selector"
    assert rebuilt_dispatch_prepare.get("workflow_selection_rationale") == (
        "selector_selected_discovered_candidate"
    )


def test_progress_summary_prefers_lifecycle_counts_over_null_history_rows() -> None:
    serialised = von_routes._serialise_tool_progress_state(
        {
            "request_id": "req-tool-summary",
            "status": "phase_transition",
            "phase": "tool_plan",
            "stage": "tool_plan",
            "phase_label": "Planning tool calls",
            "updated_at_epoch": 5500.0,
            "updated_at": "2026-03-16T03:50:59.195266Z",
            "last_activity_epoch": 5500.0,
            "last_stage_activity_epoch": 5500.0,
            "request_started_epoch": 5490.0,
            "counters": {
                "tokens_streamed": 0,
                "tools_started": 2,
                "tools_completed": 2,
            },
            "tool_call_count": 2,
            "tool_call_start_count": 2,
            "tool_call_end_count": 2,
            "tool_success_count": 0,
            "tool_failure_count": 1,
            "tool_pending_count": 0,
            "tool_history": [
                {
                    "tool": "task_get",
                    "workflowTask": "#V#chat_assistant_workflow",
                    "batchSize": 1,
                    "phase": "tool_plan",
                    "resultSummary": (
                        "Error: NOT_FOUND - Task not found: "
                        "#V#task_admin_task_seminar_junyi_c..."
                    ),
                    "success": None,
                    "callId": "call-task-get-1",
                },
                {
                    "tool": "create_concepts",
                    "workflowTask": "#V#chat_assistant_workflow",
                    "batchSize": 2,
                    "phase": "tool_plan",
                    "resultSummary": (
                        "Error: NOT_FOUND - Task not found: "
                        "#V#task_admin_task_seminar_junyi_c..."
                    ),
                    "success": None,
                    "callId": None,
                },
            ],
            "diagnostic_events": [],
        },
        now_epoch=5500.0,
    )

    assert serialised.get("tool_call_count") == 2
    assert serialised.get("tool_call_start_count") == 2
    assert serialised.get("tool_call_end_count") == 2
    assert serialised.get("tool_failure_count") == 1
    assert serialised.get("tool_pending_count") == 0

    stage_diagnostics = serialised.get("stage_diagnostics")
    assert isinstance(stage_diagnostics, list)


def test_turn_execution_diagnostics_include_routing_diagnostics_from_selector_and_dispatch() -> None:
    snapshot = von_routes._serialise_tool_progress_state(
        {
            "request_id": "req-routing-diag",
            "status": "thinking",
            "phase": "workflow_dispatch",
            "stage": "workflow_dispatch",
            "phase_label": "Workflow selected",
            "selected_workflow_id": "#V#tool_calling_workflow",
            "workflow_selector_verdict": "rag_selected",
            "workflow_selection_rationale": "selector_selected_discovered_candidate",
            "counters": {"tools_started": 0, "tools_completed": 0},
        }
    )

    diagnostics = von_routes._build_turn_execution_diagnostics(
        request_id="req-routing-diag",
        prompt_text="Run the meeting invitation test",
        tool_progress_state=snapshot,
        workflow_discovery={
            "candidate_count": 2,
            "match_count": 0,
            "candidates": [
                {
                    "concept_id": "#V#meeting_invitation_testing_workflow",
                    "routing_eligible": False,
                    "routing_exclusion_reason": "missing_authoritative_purpose",
                },
                {
                    "concept_id": "#V#tool_calling_workflow",
                    "routing_eligible": True,
                },
            ],
        },
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
            "selection_rationale": "selector_selected_discovered_candidate",
        },
        aux_llm_calls=[
            {
                "type": "workflow_selector",
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "rag_selected",
                "prompt": {"text": "Select workflow", "char_count": 15},
                "response": {
                    "text": "#V#tool_calling_workflow",
                    "char_count": 24,
                },
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "execution_mode_selected",
                "status": "selected",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
            },
        ],
    )

    routing_diagnostics = diagnostics.get("workflow_routing_diagnostics")
    assert isinstance(routing_diagnostics, dict)
    assert routing_diagnostics.get("selected_workflow_id") == "#V#tool_calling_workflow"
    assert routing_diagnostics.get("selector", {}).get("response", {}).get("text") == (
        "#V#tool_calling_workflow"
    )
    assert routing_diagnostics.get("dispatch", {}).get("selected_execution_mode") == (
        "tool_pipeline"
    )


def test_turn_execution_diagnostics_include_stage_specific_user_utility_payloads() -> None:
    snapshot = von_routes._serialise_tool_progress_state(
        {
            "request_id": "req-stage-utility",
            "status": "follow_up_required",
            "phase": "completion_gate",
            "stage": "completion_gate",
            "phase_label": "Completion gate",
            "selected_workflow_id": "#V#tool_calling_workflow",
            "selected_workflow_name": "Tool calling workflow",
            "workflow_selector_verdict": "tool_seeking",
            "workflow_selector_source": "selector",
            "workflow_stage_path": {
                "schema_version": "conversation_turn_stage_path.v1",
                "path": [
                    {
                        "stage_id": "workflow_dispatch_prepare",
                        "stage_label": "Workflow dispatch preparation",
                    },
                    {"stage_id": "tool_plan", "stage_label": "Tool-call planning"},
                    {"stage_id": "screen_backfill", "stage_label": "Screen backfill"},
                    {"stage_id": "narration", "stage_label": "Narration rendering"},
                    {
                        "stage_id": "postcondition_critic",
                        "stage_label": "Postcondition critic",
                    },
                    {"stage_id": "completion_gate", "stage_label": "Completion gate"},
                ],
            },
            "workflow_routing_diagnostics": {
                "dispatch": {
                    "pre_dispatch": {
                        "step_count": 2,
                        "completed_step_count": 2,
                        "failed_step_count": 0,
                        "total_duration_ms": 19,
                        "slowest_step_id": "load_contract",
                        "slowest_step_label": "Load workflow contract",
                        "slowest_step_duration_ms": 12,
                        "steps": [
                            {
                                "step_id": "resolve_inputs",
                                "step_label": "Resolve workflow inputs",
                                "status": "completed",
                                "duration_ms": 7,
                            },
                            {
                                "step_id": "load_contract",
                                "step_label": "Load workflow contract",
                                "status": "completed",
                                "duration_ms": 12,
                            },
                        ],
                    },
                    "tool_execution": {
                        "planned_count": 2,
                        "started_count": 1,
                        "executed_count": 1,
                        "invocation_count": 1,
                        "successful_invocation_count": 1,
                        "failed_invocation_count": 0,
                        "blocked_invocation_count": 0,
                        "worker_unavailable_event_count": 0,
                        "tool_plan_stage_event_count": 1,
                        "tool_execute_stage_event_count": 1,
                        "parse_error_invocation_count": 0,
                        "validation_error_invocation_count": 0,
                        "zero_tools_executed": False,
                        "failure_codes": [],
                    },
                }
            },
            "diagnostic_events": [
                {
                    "phase": "workflow_dispatch_prepare",
                    "status": "thinking",
                    "result_summary": "Preparing workflow dispatch",
                    "subtask": "Resolve workflow inputs",
                },
                {
                    "phase": "tool_plan",
                    "status": "thinking",
                    "workflow_task": "fetch_concept",
                },
                {
                    "phase": "completion_gate",
                    "status": "follow_up_required",
                    "result_summary": "Follow-up required before completion.",
                },
            ],
        }
    )

    diagnostics = von_routes._build_turn_execution_diagnostics(
        request_id="req-stage-utility",
        prompt_text="Represent this uploaded paper",
        tool_progress_state=snapshot,
        response_transformations={
            "schema_version": "response_transformations.v1",
            "transformations": [
                {
                    "transform_name": "screen_backfill",
                    "status": "fallback_success",
                    "source_path": "response_text_plus_follow_up_summary",
                    "latency_ms": 44,
                    "model_id": "gpt-4.1-mini",
                    "input_summary": {
                        "needs_backfill": True,
                        "tool_message_count": 3,
                    },
                    "output_summary": {
                        "applied": True,
                        "presenter_format": (
                            "screen_backfill_from_response_with_operational_summary_v1"
                        ),
                    },
                },
                {
                    "transform_name": "spoken_backfill",
                    "status": "fallback_success",
                    "source_path": "screen_text_fallback",
                    "latency_ms": 18,
                    "model_id": "gpt-4.1-mini",
                    "input_summary": {
                        "needs_backfill": True,
                        "tool_message_count": 3,
                    },
                    "output_summary": {
                        "applied": True,
                        "presenter_format": "narration_fallback_v1",
                    },
                },
            ],
        },
        critic_verdict={
            "workflow_id": "#V#kb_mutation_postcondition_critic_workflow",
            "has_unresolved_checks": True,
            "unresolved_check_count": 2,
            "summary": {
                "verified_count": 1,
                "not_verified_count": 1,
                "inconclusive_count": 1,
                "error_count": 0,
            },
        },
        completion_gate_verdict={
            "decision": "follow_up_required",
            "decision_reason": "required_effects_unresolved",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "blocking_effect_ids": ["effect_tool_execution_1"],
            "blocking_failure_codes": [
                "tool_execution_required_but_not_observed"
            ],
        },
    )

    assert diagnostics.get("completion_gate") == {
        "decision": "follow_up_required",
        "decision_reason": "required_effects_unresolved",
        "requires_follow_up": True,
        "safe_to_claim_completion": False,
        "blocking_effect_ids": ["effect_tool_execution_1"],
        "blocking_failure_codes": ["tool_execution_required_but_not_observed"],
    }
    assert diagnostics.get("critic_verdict") == {
        "workflow_id": "#V#kb_mutation_postcondition_critic_workflow",
        "has_unresolved_checks": True,
        "unresolved_check_count": 2,
        "summary": {
            "verified_count": 1,
            "not_verified_count": 1,
            "inconclusive_count": 1,
            "error_count": 0,
        },
    }

    stage_diagnostics = diagnostics.get("stage_diagnostics")
    assert isinstance(stage_diagnostics, list)
    by_stage = {
        entry.get("stage_id"): entry
        for entry in stage_diagnostics
        if isinstance(entry, dict) and isinstance(entry.get("stage_id"), str)
    }

    assert by_stage["workflow_dispatch_prepare"]["selected_workflow_id"] == (
        "#V#tool_calling_workflow"
    )
    assert by_stage["workflow_dispatch_prepare"]["pre_dispatch"]["slowest_step_label"] == (
        "Load workflow contract"
    )
    assert by_stage["workflow_dispatch_prepare"]["latest_subtask"] == (
        "Resolve workflow inputs"
    )
    assert by_stage["tool_plan"]["tool_execution"]["planned_count"] == 2
    assert by_stage["screen_backfill"]["response_transformation"] == {
        "transform_name": "screen_backfill",
        "status": "fallback_success",
        "source_path": "response_text_plus_follow_up_summary",
        "latency_ms": 44,
        "model_id": "gpt-4.1-mini",
        "suppression_reason": None,
        "error_class": None,
        "input_summary": {
            "needs_backfill": True,
            "tool_message_count": 3,
        },
        "output_summary": {
            "applied": True,
            "presenter_format": (
                "screen_backfill_from_response_with_operational_summary_v1"
            ),
        },
    }
    assert by_stage["narration"]["response_transformation"] == {
        "transform_name": "spoken_backfill",
        "status": "fallback_success",
        "source_path": "screen_text_fallback",
        "latency_ms": 18,
        "model_id": "gpt-4.1-mini",
        "suppression_reason": None,
        "error_class": None,
        "input_summary": {
            "needs_backfill": True,
            "tool_message_count": 3,
        },
        "output_summary": {
            "applied": True,
            "presenter_format": "narration_fallback_v1",
        },
    }
    assert by_stage["postcondition_critic"]["critic_verdict"]["unresolved_check_count"] == 2
    assert by_stage["completion_gate"]["completion_gate"]["blocking_effect_ids"] == [
        "effect_tool_execution_1"
    ]


def test_turn_execution_diagnostics_clear_stale_selector_prompt_failure_after_success(
    monkeypatch,
) -> None:
    clock = _set_clock(monkeypatch, start=5610.0)

    selected_workflow_id = "#V#meeting_invitation_testing_workflow"
    selector_prompt_entry = {
        "type": "workflow_selector_prompt",
        "prompt_id": "#V#chat_turn_classifier_prompt",
        "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
        "prompt": {"text": "Select workflow", "char_count": 15},
        "candidate_list": {"text": f"- {selected_workflow_id}", "char_count": 39},
        "prompt_failure_reason": "selector_prompt_missing_candidate_list",
        "prompt_failure_detail": "Selector template omitted the candidate list.",
    }
    selector_success_entry = {
        "type": "workflow_selector",
        "workflow_id": selected_workflow_id,
        "verdict": "rag_selected",
        "selection_source": "selector",
        "response": {"text": selected_workflow_id, "char_count": 38},
        "selection_rationale": "selector_selected_discovered_candidate",
    }

    von_routes._set_tool_progress(
        "scope-custom-workflow",
        "req-custom-workflow",
        {
            "status": "thinking",
            "phase": "workflow_dispatch",
            "phase_label": "Workflow selected",
            "request_id": "req-custom-workflow",
            "selected_workflow_id": selected_workflow_id,
            "selected_workflow_name": "Meeting invitation testing workflow",
            "workflow_selector_verdict": "rag_selected",
            "workflow_selector_source": "selector",
            "workflow_selection_rationale": "selector_selected_discovered_candidate",
            "workflow_routing": {
                "workflow_id": selected_workflow_id,
                "verdict": "rag_selected",
                "source": "selector",
                "selection_rationale": "selector_selected_discovered_candidate",
            },
            "workflow_routing_aux": [selector_prompt_entry, selector_success_entry],
            "counters": {"tools_started": 0, "tools_completed": 0},
        },
    )
    clock["now"] += 0.1
    von_routes._set_tool_progress(
        "scope-custom-workflow",
        "req-custom-workflow",
        {
            "status": "thinking",
            "phase": "workflow_dispatch",
            "phase_label": "Workflow terminal state",
            "request_id": "req-custom-workflow",
            "selected_workflow_id": selected_workflow_id,
            "selected_workflow_name": "Meeting invitation testing workflow",
            "workflow_selector_verdict": "rag_selected",
            "workflow_selector_source": "selector",
            "workflow_selection_rationale": "selector_selected_discovered_candidate",
            "result_summary": "workflow_terminal:failed",
            "workflow_routing": {
                "workflow_id": selected_workflow_id,
                "verdict": "rag_selected",
                "source": "selector",
                "selection_rationale": "selector_selected_discovered_candidate",
            },
            "workflow_routing_aux": [
                selector_prompt_entry,
                selector_success_entry,
                {
                    "type": "workflow_dispatch_boundary",
                    "boundary": "execution_mode_selected",
                    "status": "selected",
                    "selected_execution_mode": "custom_workflow",
                    "selected_workflow_id": selected_workflow_id,
                    "dispatch_workflow_id": selected_workflow_id,
                },
                {
                    "type": "workflow_dispatch_boundary",
                    "boundary": "workflow_handoff",
                    "status": "started",
                    "selected_execution_mode": "custom_workflow",
                    "selected_workflow_id": selected_workflow_id,
                    "dispatch_workflow_id": selected_workflow_id,
                },
                {
                    "type": "workflow_dispatch_boundary",
                    "boundary": "workflow_terminal",
                    "status": "failed",
                    "selected_execution_mode": "custom_workflow",
                    "selected_workflow_id": selected_workflow_id,
                    "dispatch_workflow_id": selected_workflow_id,
                    "final_state": "prepare_spec",
                    "completed": False,
                    "reason": "workflow_launch_input_resolution_failed",
                    "detail": (
                        "Workflow #V#meeting_invitation_testing_workflow could not "
                        "start because required launch inputs were unresolved: "
                        "invitation_text."
                    ),
                    "workflow_launch_input_resolution_status": "failed",
                    "unresolved_required_inputs": ["invitation_text"],
                    "failing_state_id": "prepare_spec",
                    "failing_action_id": "tool.prepare_spec",
                },
            ],
            "counters": {"tools_started": 0, "tools_completed": 0},
        },
    )

    snapshot = von_routes._snapshot_tool_progress_for_request(
        "scope-custom-workflow",
        "req-custom-workflow",
    )
    assert snapshot is not None

    diagnostics = von_routes._build_turn_execution_diagnostics(
        request_id="req-custom-workflow",
        prompt_text="Run the meeting invitation testing workflow",
        tool_progress_state=snapshot,
    )

    stage_diagnostics = diagnostics.get("stage_diagnostics")
    assert isinstance(stage_diagnostics, list)
    workflow_dispatch = next(
        (
            entry
            for entry in stage_diagnostics
            if isinstance(entry, dict) and entry.get("stage_id") == "workflow_dispatch"
        ),
        None,
    )
    assert workflow_dispatch is not None
    assert workflow_dispatch.get("selected_workflow_id") == selected_workflow_id
    assert workflow_dispatch.get("selected_workflow_name") == (
        "Meeting invitation testing workflow"
    )
    assert workflow_dispatch.get("workflow_selector_verdict") == "rag_selected"
    assert workflow_dispatch.get("workflow_selector_source") == "selector"
    assert workflow_dispatch.get("workflow_selection_rationale") == (
        "selector_selected_discovered_candidate"
    )
    assert workflow_dispatch.get("terminal_state") == "failure"
    assert workflow_dispatch.get("dispatch_terminal_status") == "failed"
    assert workflow_dispatch.get("dispatch_terminal_failure_reason") == (
        "workflow_launch_input_resolution_failed"
    )
    assert workflow_dispatch.get("llm_exchange_record_count") == 2
    assert workflow_dispatch.get("llm_exchange_entry_types") == [
        "workflow_selector_prompt",
        "workflow_selector",
    ]
    assert workflow_dispatch.get("latest_llm_exchange", {}).get("entry_type") == (
        "workflow_selector"
    )
    assert workflow_dispatch.get("latest_llm_exchange", {}).get(
        "response_preview", {}
    ).get("text") == selected_workflow_id

    activity_history = diagnostics.get("activity_history")
    assert isinstance(activity_history, list)
    assert activity_history[-1].get("stage") == "workflow_dispatch"
    assert activity_history[-1].get("state") == "failure"

    routing_diagnostics = diagnostics.get("workflow_routing_diagnostics")
    assert isinstance(routing_diagnostics, dict)
    assert routing_diagnostics.get("selected_workflow_id") == selected_workflow_id
    assert routing_diagnostics.get("selector", {}).get("prompt_failure_reason") is None
    assert routing_diagnostics.get("selector", {}).get("prompt_failure_detail") is None
    assert routing_diagnostics.get("dispatch", {}).get("selected_execution_mode") == (
        "custom_workflow"
    )
    assert routing_diagnostics.get("dispatch", {}).get(
        "dispatch_terminal_failure_reason"
    ) == "workflow_launch_input_resolution_failed"
    assert routing_diagnostics.get("dispatch", {}).get(
        "dispatch_terminal_failing_state_id"
    ) == "prepare_spec"
    assert routing_diagnostics.get("dispatch", {}).get(
        "dispatch_terminal_failing_action_id"
    ) == "tool.prepare_spec"
    assert routing_diagnostics.get("dispatch", {}).get(
        "dispatch_terminal_unresolved_required_inputs"
    ) == ["invitation_text"]


def test_serialised_tool_progress_state_includes_live_workflow_routing_diagnostics() -> None:
    serialised = von_routes._serialise_tool_progress_state(
        {
            "request_id": "req-live-routing-diag",
            "status": "thinking",
            "phase": "workflow_dispatch",
            "stage": "workflow_dispatch",
            "phase_label": "Workflow selected",
            "selected_workflow_id": "#V#tool_calling_workflow",
            "workflow_discovery": {
                "candidate_count": 3,
                "match_count": 0,
                "candidates": [
                    {
                        "concept_id": "#V#chat_assistant_workflow",
                        "candidate_source": "selector_default",
                        "routing_eligible": True,
                    },
                    {
                        "concept_id": "#V#tool_calling_workflow",
                        "candidate_source": "selector_default",
                        "routing_eligible": True,
                    },
                    {
                        "concept_id": "#V#meeting_invitation_testing_workflow",
                        "candidate_source": "workflow_discovery",
                        "routing_eligible": False,
                        "routing_exclusion_reason": "missing_authoritative_purpose",
                    },
                ],
            },
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "rag_selected",
                "source": "selector",
                "selection_rationale": "selector_selected_discovered_candidate",
            },
            "workflow_routing_aux": [
                {
                    "type": "workflow_selector_prompt",
                    "prompt_id": "#V#chat_turn_classifier_prompt",
                    "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
                    "prompt": {"text": "Select workflow", "char_count": 15},
                    "candidate_list": {
                        "text": "- #V#tool_calling_workflow",
                        "char_count": 24,
                    },
                    "candidate_entries": [
                        {
                            "concept_id": "#V#chat_assistant_workflow",
                            "candidate_source": "selector_default",
                        },
                        {
                            "concept_id": "#V#tool_calling_workflow",
                            "candidate_source": "selector_default",
                        },
                        {
                            "concept_id": "#V#meeting_invitation_testing_workflow",
                            "candidate_source": "workflow_discovery",
                            "candidate_reason": "discovered_workflow_candidate",
                        },
                    ],
                },
                {
                    "type": "workflow_model_policy_stage",
                    "stage": "workflow_dispatch",
                    "policy_stage": "classifier",
                    "request": {
                        "prompt": {"text": "Select workflow", "char_count": 15},
                        "context_messages": [
                            {
                                "role": "system",
                                "content": {
                                    "text": "Workflow selector prompt",
                                    "char_count": 24,
                                },
                            }
                        ],
                        "context_message_count": 1,
                    },
                    "fallback_used": True,
                    "fallback_attempt_count": 2,
                    "failure_count": 1,
                    "fallback_attempts": [
                        {
                            "attempt_no": 1,
                            "provider": "ollama",
                            "model": "granite3.3:2b",
                            "status": "failed",
                            "failure_kind": "provider_unreachable",
                        },
                        {
                            "attempt_no": 2,
                            "provider": "openai",
                            "model": "gpt-5-mini",
                            "status": "succeeded",
                            "response": {
                                "text": "#V#tool_calling_workflow",
                                "char_count": 24,
                            },
                        },
                    ],
                },
                {
                    "type": "workflow_selector",
                    "workflow_id": "#V#tool_calling_workflow",
                    "verdict": "rag_selected",
                    "selection_source": "selector",
                    "response": {
                        "text": "#V#tool_calling_workflow",
                        "char_count": 24,
                    },
                    "selection_metadata": {
                        "selection_resolution": "candidate_label_exact_match",
                    },
                },
            ],
            "counters": {"tools_started": 0, "tools_completed": 0},
        }
    )

    routing_diagnostics = serialised.get("workflow_routing_diagnostics")
    assert isinstance(routing_diagnostics, dict)
    assert routing_diagnostics["selector"]["candidate_source_counts"] == [
        {"name": "selector_default", "count": 2},
        {"name": "workflow_discovery", "count": 1},
    ]
    assert routing_diagnostics["selector"]["model_attempts"][0]["failure_kind"] == (
        "provider_unreachable"
    )
    assert routing_diagnostics["selector"]["model_request"]["prompt"]["text"] == (
        "Select workflow"
    )
    assert routing_diagnostics["selector"]["primary_fallback_failure_kind"] == (
        "provider_unreachable"
    )
    assert routing_diagnostics["selector"]["selection_resolution"] == (
        "candidate_label_exact_match"
    )
