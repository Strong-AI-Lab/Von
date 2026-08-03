"""Outcome-focused tests for live turn progress and observational telemetry."""

import os
import time
from types import SimpleNamespace

from flask import Flask

from src.backend.server.routes import von_routes
from src.backend.services.thinking_semantic_projection_service import (
    build_semantic_operation_projection,
)
from src.backend.services.tool_progress_store_service import (
    clear_tool_progress_documents_for_tests,
    flush_queued_tool_progress_states,
    queued_tool_progress_state_count,
    reset_tool_progress_persistence_queue_for_tests,
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
    reset_tool_progress_persistence_queue_for_tests()
    clear_tool_progress_documents_for_tests()


def _progress_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    return app


def _relation_semantic_operation(
    *,
    lifecycle_status: str,
    success: bool | None = None,
    result: dict | None = None,
) -> dict:
    return build_semantic_operation_projection(
        operation_id="call-semantic-relation",
        capability_name="add_relationship",
        execution_method="add_relationship",
        capability_kind="registered_tool",
        arguments={
            "source_id": "#V#nathan_young_doctoral_candidature_situation",
            "predicate": "#V#has_doctoral_supervisor",
            "target": "#V#robert_amor",
        },
        lifecycle_status=lifecycle_status,
        success=success,
        result=result,
    )


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


def test_heartbeat_updates_sequence_and_keeps_current_activity(monkeypatch) -> None:
    clock = _set_clock(monkeypatch)

    von_routes._set_tool_progress(
        "scope-a",
        "req-a",
        {
            "status": "thinking",
            "phase": "tool_execute",
            "phase_label": "Reading evidence",
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
        {"status": "heartbeat", "request_id": "req-a"},
    )

    heartbeat = von_routes._get_tool_progress("scope-a", "req-a")
    assert heartbeat is not None
    assert heartbeat["sequence_no"] == 2
    assert heartbeat["event_kind"] == "heartbeat"
    assert heartbeat["stage"] == "tool_execute"
    assert heartbeat["idle_ms"] >= int(
        von_routes._TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC * 1000
    )


def test_observational_progress_does_not_synthesise_a_workflow_stage_path(
    monkeypatch,
) -> None:
    clock = _set_clock(monkeypatch)
    scope_key = "scope-observational"
    request_id = "req-observational"

    von_routes._set_tool_progress(
        scope_key,
        request_id,
        {
            "record_kind": "observational",
            "status": "thinking",
            "phase": "context_build",
            "request_id": request_id,
        },
    )
    clock["now"] += 0.1
    von_routes._set_tool_progress(
        scope_key,
        request_id,
        {
            "record_kind": "observational",
            "status": "llm_call_start",
            "phase": "model_call",
            "request_id": request_id,
        },
    )

    state = von_routes._get_tool_progress(scope_key, request_id)
    assert state is not None
    assert state["record_kind"] == "observational"
    assert "workflow_stage_path" not in state

    serialised = von_routes._serialise_tool_progress_state(
        state,
        now_epoch=clock["now"],
    )
    assert serialised["record_kind"] == "observational"
    assert "workflow_stage_path" not in serialised
    assert [entry["phase"] for entry in serialised["phase_history"]] == [
        "context_build",
        "model_call",
    ]


def test_serialisation_preserves_an_explicit_historical_workflow_stage_path(
    monkeypatch,
) -> None:
    _set_clock(monkeypatch)
    historical_path = {
        "schema_version": "conversation_turn_stage_path.v1",
        "workflow_representation_id": "#V#conversation_turn_execution_workflow",
        "path": [],
    }
    von_routes._set_tool_progress(
        "scope-historical-path",
        "req-historical-path",
        {
            "status": "thinking",
            "phase": "workflow_routing",
            "request_id": "req-historical-path",
            "workflow_stage_path": historical_path,
        },
    )

    state = von_routes._get_tool_progress(
        "scope-historical-path",
        "req-historical-path",
    )
    assert state is not None
    assert von_routes._serialise_tool_progress_state(state)[
        "workflow_stage_path"
    ] == historical_path


def test_heartbeat_reconciles_a_terminal_background_result(monkeypatch) -> None:
    monkeypatch.setattr(von_routes, "_TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC", 0.01)
    request_id = "req-terminal-task-result"
    scope_key = "scope-terminal-task-result"
    terminal_status = SimpleNamespace(
        status="completed",
        progress={
            "status": "completed",
            "source": "background_generate_success_body",
            "result_summary": "Completed result exists.",
        },
        error=None,
    )

    class _FakeBackgroundTaskRegistry:
        def get_task_status(self, task_id):
            return terminal_status if task_id == request_id else None

    monkeypatch.setattr(
        von_routes,
        "background_task_registry",
        _FakeBackgroundTaskRegistry(),
    )
    von_routes._set_tool_progress(
        scope_key,
        request_id,
        {
            "status": "heartbeat",
            "phase": "response_finalising",
            "phase_label": "Finalising response",
            "request_id": request_id,
        },
    )

    stop_event, thread = von_routes._start_tool_progress_heartbeat(
        [scope_key],
        request_id,
    )
    try:
        for _ in range(50):
            state = von_routes._get_tool_progress(scope_key, request_id)
            if state and state.get("status") == "completed":
                break
            time.sleep(0.01)
    finally:
        von_routes._stop_tool_progress_heartbeat(stop_event, thread)

    state = von_routes._get_tool_progress(scope_key, request_id)
    assert state is not None
    assert state["status"] == "completed"
    assert state["source"] == "background_task_terminal_reconciliation"
    assert state["terminal_task_progress"]["source"] == (
        "background_generate_success_body"
    )
    assert not thread.is_alive()


def test_progress_endpoint_reads_persisted_state_after_local_cache_miss(
    monkeypatch,
) -> None:
    app = _progress_app()
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: True,
    )

    scope_key = "user:#V#test_user"
    semantic_operation = _relation_semantic_operation(lifecycle_status="running")
    von_routes._set_tool_progress(
        scope_key,
        "req-persisted",
        {
            "status": "thinking",
            "phase": "evidence_read",
            "phase_label": "Reading evidence",
            "request_id": "req-persisted",
            "goal_label": "Summarise the represented evidence",
            "semantic_operation": semantic_operation,
        },
    )
    flush_queued_tool_progress_states(force=True)
    with von_routes._TOOL_PROGRESS_LOCK:
        von_routes._TOOL_PROGRESS.clear()

    response = app.test_client().get("/von/progress/req-persisted")
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("request_id") == "req-persisted"
    assert body.get("stage") == "evidence_read"
    assert body.get("phase_label") == "Reading evidence"
    assert body.get("goal_label") == "Summarise the represented evidence"
    assert body.get("semantic_operation") == semantic_operation
    assert body["diagnostic_events"][-1]["semantic_operation"] == semantic_operation


def test_get_tool_progress_prefers_live_memory_before_persisted_fetch(
    monkeypatch,
) -> None:
    von_routes._set_tool_progress(
        "scope-live",
        "req-live",
        {
            "status": "thinking",
            "phase": "tool_execute",
            "phase_label": "Reading evidence",
            "request_id": "req-live",
        },
    )

    def _unexpected_fetch(*, scope_key: str, request_id: str):
        raise AssertionError(
            "persisted progress fetch should not run while live state exists"
        )

    monkeypatch.setattr(von_routes, "fetch_tool_progress_state", _unexpected_fetch)
    state = von_routes._get_tool_progress("scope-live", "req-live")
    assert state is not None
    assert state["request_id"] == "req-live"
    assert state["phase"] == "tool_execute"


def test_non_terminal_progress_is_batched_until_flush(monkeypatch) -> None:
    writes: list[dict] = []

    def _fake_store(*, scope_key, request_id, payload, ttl_seconds):
        writes.append(dict(payload))
        return True

    monkeypatch.setenv("VON_TOOL_PROGRESS_PERSISTENCE_FLUSH_INTERVAL_SECONDS", "60")
    monkeypatch.setattr(
        "src.backend.services.tool_progress_store_service.store_tool_progress_state",
        _fake_store,
    )

    von_routes._set_tool_progress(
        "scope-batched",
        "req-batched",
        {
            "status": "thinking",
            "phase": "model_call",
            "request_id": "req-batched",
        },
    )
    von_routes._set_tool_progress(
        "scope-batched",
        "req-batched",
        {"status": "heartbeat", "request_id": "req-batched"},
    )

    assert writes == []
    assert queued_tool_progress_state_count() == 1
    state = von_routes._get_tool_progress("scope-batched", "req-batched")
    assert state is not None
    assert state["sequence_no"] == 2
    assert state["progress_persistence"]["status"] == "queued"

    summary = flush_queued_tool_progress_states(force=True)
    assert summary["flushed_count"] == 1
    assert queued_tool_progress_state_count() == 0
    assert len(writes) == 1
    assert writes[0]["sequence_no"] == 2
    assert writes[0]["progress_persistence"]["status"] == "flushed"


def test_terminal_progress_flushes_immediately(monkeypatch) -> None:
    writes: list[dict] = []

    def _fake_store(*, scope_key, request_id, payload, ttl_seconds):
        writes.append(dict(payload))
        return True

    monkeypatch.setenv("VON_TOOL_PROGRESS_PERSISTENCE_FLUSH_INTERVAL_SECONDS", "60")
    monkeypatch.setattr(
        "src.backend.services.tool_progress_store_service.store_tool_progress_state",
        _fake_store,
    )

    von_routes._set_tool_progress(
        "scope-terminal",
        "req-terminal",
        {"status": "thinking", "request_id": "req-terminal"},
    )
    von_routes._set_tool_progress(
        "scope-terminal",
        "req-terminal",
        {"status": "completed", "request_id": "req-terminal"},
    )

    assert len(writes) == 1
    assert writes[0]["status"] == "completed"
    assert writes[0]["progress_persistence"]["flush_reason"] == "terminal_state"
    state = von_routes._get_tool_progress("scope-terminal", "req-terminal")
    assert state is not None
    assert state["progress_persistence"]["status"] == "flushed"


def test_terminal_progress_ignores_all_late_updates(monkeypatch) -> None:
    _set_clock(monkeypatch)
    writes: list[dict] = []

    def _fake_store(*, scope_key, request_id, payload, ttl_seconds):
        writes.append(dict(payload))
        return True

    monkeypatch.setattr(
        "src.backend.services.tool_progress_store_service.store_tool_progress_state",
        _fake_store,
    )

    for terminal_status in ("completed", "cancelled", "error"):
        scope_key = f"scope-immutable-{terminal_status}"
        request_id = f"req-immutable-{terminal_status}"
        von_routes._set_tool_progress(
            scope_key,
            request_id,
            {
                "status": "thinking",
                "phase": "tool_execute",
                "request_id": request_id,
            },
        )
        von_routes._set_tool_progress(
            scope_key,
            request_id,
            {
                "status": terminal_status,
                "phase": terminal_status,
                "request_id": request_id,
                "result_summary": f"Terminal {terminal_status} evidence.",
            },
        )
        terminal = von_routes._get_tool_progress(scope_key, request_id)
        assert terminal is not None
        terminal_sequence = terminal["sequence_no"]
        terminal_events = list(terminal["diagnostic_events"])
        writes_after_terminal = len(writes)

        von_routes._set_tool_progress(
            scope_key,
            request_id,
            {
                "status": "tool_invoked",
                "phase": "tool_execute",
                "request_id": request_id,
                "tool": "late_read",
                "result_summary": "Late payload that must be ignored.",
            },
        )

        after_late_update = von_routes._get_tool_progress(scope_key, request_id)
        assert after_late_update is not None
        assert after_late_update["status"] == terminal_status
        assert after_late_update["phase"] == terminal_status
        assert after_late_update["sequence_no"] == terminal_sequence
        assert after_late_update["diagnostic_events"] == terminal_events
        assert after_late_update["result_summary"] == (
            f"Terminal {terminal_status} evidence."
        )
        assert len(writes) == writes_after_terminal


def test_failed_terminal_flush_remains_pending_for_retry(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.backend.services.tool_progress_store_service.store_tool_progress_state",
        lambda **_kwargs: False,
    )

    von_routes._set_tool_progress(
        "scope-failed-flush",
        "req-failed-flush",
        {"status": "completed", "request_id": "req-failed-flush"},
    )

    state = von_routes._get_tool_progress("scope-failed-flush", "req-failed-flush")
    assert state is not None
    persistence = state["progress_persistence"]
    assert persistence["status"] == "failed"
    assert persistence["failed_flush_count"] == 1
    assert queued_tool_progress_state_count() == 1


def test_synchronous_durability_opt_in_flushes(monkeypatch) -> None:
    writes: list[dict] = []

    def _fake_store(*, scope_key, request_id, payload, ttl_seconds):
        writes.append(dict(payload))
        return True

    monkeypatch.setenv("VON_TOOL_PROGRESS_PERSISTENCE_FLUSH_INTERVAL_SECONDS", "60")
    monkeypatch.setattr(
        "src.backend.services.tool_progress_store_service.store_tool_progress_state",
        _fake_store,
    )
    von_routes._set_tool_progress(
        "scope-sync",
        "req-sync",
        {
            "status": "thinking",
            "request_id": "req-sync",
            "progress_persistence_synchronous": True,
        },
    )

    assert len(writes) == 1
    assert writes[0]["progress_persistence"]["flush_reason"] == (
        "synchronous_durability"
    )
    assert writes[0]["progress_persistence"]["synchronous_durability"] is True


def test_progress_endpoint_returns_explanatory_pending_payload(monkeypatch) -> None:
    app = _progress_app()
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: True,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: None,
    )

    response = app.test_client().get("/von/progress/req-pending")
    assert response.status_code == 202
    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("status") == "pending"
    assert body.get("pending_reason") == "no_visible_progress_state"
    assert "No live progress state is visible yet" in str(body.get("result_summary"))


def test_progress_endpoint_uses_window_scope_for_anonymous_requests(
    monkeypatch,
) -> None:
    app = _progress_app()
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: True,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: None,
    )
    window_session_id = "ws_progress_visibility"

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
            "phase": "evidence_read",
            "phase_label": "Reading evidence",
            "request_id": "req-window-scope",
        },
    )
    flush_queued_tool_progress_states(force=True)
    with von_routes._TOOL_PROGRESS_LOCK:
        von_routes._TOOL_PROGRESS.clear()

    response = app.test_client().get(
        "/von/progress/req-window-scope",
        headers={"X-Von-Window-Session": window_session_id},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("stage") == "evidence_read"
    assert body.get("phase_label") == "Reading evidence"


def test_progress_endpoint_uses_header_user_scope_without_session_lookup(
    monkeypatch,
) -> None:
    app = _progress_app()
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: True,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: (_ for _ in ()).throw(
            AssertionError("header-scoped progress should not resolve session auth")
        ),
    )
    request_id = "req-header-user-scope"
    von_routes._set_tool_progress(
        "user:#V#test_user",
        request_id,
        {
            "status": "thinking",
            "phase": "model_call",
            "phase_label": "Generating response",
            "request_id": request_id,
        },
    )

    response = app.test_client().get(
        f"/von/progress/{request_id}",
        headers={"X-User-Concept-ID": "#V#test_user"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("stage") == "model_call"
    assert body.get("phase_label") == "Generating response"


def test_progress_endpoint_uses_window_header_without_session_lookup(
    monkeypatch,
) -> None:
    app = _progress_app()
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: True,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: (_ for _ in ()).throw(
            AssertionError("window-scoped progress should not resolve session auth")
        ),
    )
    request_id = "req-header-window-scope"
    window_session_id = "ws_header_only_progress"
    von_routes._set_tool_progress(
        f"anon:window:{window_session_id}",
        request_id,
        {
            "status": "thinking",
            "phase": "evidence_read",
            "phase_label": "Reading evidence",
            "request_id": request_id,
        },
    )

    response = app.test_client().get(
        f"/von/progress/{request_id}",
        headers={
            "X-Von-Window-Session": window_session_id,
            "X-User-Concept-ID": "#V#test_user",
        },
    )
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("stage") == "evidence_read"


def test_progress_endpoint_recovers_window_scope_after_auth_scope_shift(
    monkeypatch,
) -> None:
    app = _progress_app()
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: True,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    window_session_id = "ws_progress_auth_scope_shift"
    request_id = "req-window-auth-shift"
    von_routes._set_tool_progress(
        f"anon:window:{window_session_id}",
        request_id,
        {
            "status": "thinking",
            "phase": "model_call",
            "phase_label": "Generating response",
            "request_id": request_id,
        },
    )
    flush_queued_tool_progress_states(force=True)
    with von_routes._TOOL_PROGRESS_LOCK:
        von_routes._TOOL_PROGRESS.clear()

    response = app.test_client().get(
        f"/von/progress/{request_id}",
        headers={
            "X-Von-Window-Session": window_session_id,
            "X-User-Concept-ID": "#V#test_user",
        },
    )
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("stage") == "model_call"
    assert body.get("resolved_scope_key") == f"anon:window:{window_session_id}"
    assert body.get("progress_source") == "alternate_scope_fallback"


def test_scope_alias_registration_copies_current_state(monkeypatch) -> None:
    _set_clock(monkeypatch, start=5000.0)
    primary_scope_key = "anon:window:ws_scope_alias"
    request_id = "req-scope-alias"
    von_routes._set_tool_progress(
        primary_scope_key,
        request_id,
        {
            "status": "thinking",
            "phase": "evidence_read",
            "phase_label": "Reading evidence",
            "request_id": request_id,
        },
    )

    returned_scope_keys = von_routes._register_tool_progress_scope_aliases(
        request_id=request_id,
        primary_scope_key=primary_scope_key,
        mirror_scope_keys=[],
        user_concept_id="#V#test_user",
        window_session_id="ws_scope_alias",
        anonymous_session_id="legacy_cookie_scope",
    )

    assert returned_scope_keys == [
        "user:#V#test_user",
        "anon:session:legacy_cookie_scope",
    ]
    for scope_key in returned_scope_keys:
        state = von_routes._get_tool_progress(scope_key, request_id)
        assert state is not None
        assert state.get("stage") == "evidence_read"


def test_missing_heartbeat_marks_request_stalled(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=2000.0)
    von_routes._set_tool_progress(
        "scope-stalled",
        "req-stalled",
        {
            "status": "thinking",
            "phase": "tool_execute",
            "request_id": "req-stalled",
        },
    )
    stored = von_routes._get_tool_progress("scope-stalled", "req-stalled")
    assert stored is not None

    clock["now"] += float(von_routes._TOOL_PROGRESS_STALL_THRESHOLD_SEC + 1)
    serialised = von_routes._serialise_tool_progress_state(
        stored,
        now_epoch=clock["now"],
    )
    assert serialised["liveness_state"] == "stalled"
    assert serialised["stall_detected"] is True
    assert serialised["liveness_reason"] in {
        "tool_timeout",
        "model_timeout",
        "worker_unavailable",
        "network_silence",
    }


def test_stalled_state_recovers_after_new_activity(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=3000.0)
    von_routes._set_tool_progress(
        "scope-recovered",
        "req-recovered",
        {
            "status": "thinking",
            "phase": "tool_execute",
            "request_id": "req-recovered",
        },
    )
    stalled_source = von_routes._get_tool_progress(
        "scope-recovered",
        "req-recovered",
    )
    assert stalled_source is not None
    clock["now"] += float(von_routes._TOOL_PROGRESS_STALL_THRESHOLD_SEC + 2)
    assert (
        von_routes._serialise_tool_progress_state(
            stalled_source,
            now_epoch=clock["now"],
        )["liveness_state"]
        == "stalled"
    )

    clock["now"] += 1.0
    von_routes._set_tool_progress(
        "scope-recovered",
        "req-recovered",
        {
            "status": "tool_call_start",
            "tool": "read_evidence",
            "request_id": "req-recovered",
        },
    )
    recovered_source = von_routes._get_tool_progress(
        "scope-recovered",
        "req-recovered",
    )
    assert recovered_source is not None
    recovered = von_routes._serialise_tool_progress_state(
        recovered_source,
        now_epoch=clock["now"],
    )
    assert recovered["liveness_state"] == "active"
    assert recovered["sequence_no"] >= 2
    assert recovered["stage"] == "tool_execute"


def test_observational_events_retain_model_and_tool_evidence(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=4000.0)
    von_routes._set_tool_progress(
        "scope-observation",
        "req-observation",
        {
            "status": "llm_call_end",
            "stage": "model_call",
            "request_id": "req-observation",
            "call_id": "llm-abc123:attempt:1",
            "llm_exchange_id": "llm-abc123",
            "llm_request_state": "completed",
            "llm_request": {
                "prompt": {"text": "Summarise this evidence", "char_count": 23}
            },
            "llm_response_preview": {"text": "Summary", "char_count": 7},
            "model": "test-model",
            "provider": "openai",
            "duration_ms": 250,
            "success": True,
        },
    )
    clock["now"] += 0.1
    von_routes._set_tool_progress(
        "scope-observation",
        "req-observation",
        {
            "status": "tool_invoked",
            "phase": "tool_execute",
            "tool": "read_evidence",
            "call_id": "tool-abc123",
            "result_summary": "One evidence item read.",
            "request_id": "req-observation",
        },
    )

    state = von_routes._get_tool_progress("scope-observation", "req-observation")
    assert state is not None
    serialised = von_routes._serialise_tool_progress_state(
        state,
        now_epoch=clock["now"],
    )
    events = serialised.get("diagnostic_events")
    assert isinstance(events, list)
    assert events[0]["llm_exchange_id"] == "llm-abc123"
    assert events[0]["llm_request"]["prompt"]["text"] == "Summarise this evidence"
    assert events[-1]["tool"] == "read_evidence"
    assert events[-1]["result_summary"] == "One evidence item read."
    assert serialised["tool_call_count"] == 1
    assert serialised["tool_success_count"] == 1


def test_successful_update_clears_stale_error(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=4500.0)
    von_routes._set_tool_progress(
        "scope-stale-error",
        "req-stale-error",
        {
            "status": "llm_call_end",
            "stage": "model_call",
            "request_id": "req-stale-error",
            "success": False,
            "error": "Provider unavailable",
            "error_class": "RuntimeError",
            "failure_kind": "provider_unreachable",
        },
    )
    clock["now"] += 0.2
    von_routes._set_tool_progress(
        "scope-stale-error",
        "req-stale-error",
        {
            "status": "llm_call_end",
            "stage": "model_call",
            "request_id": "req-stale-error",
            "success": True,
        },
    )

    state = von_routes._get_tool_progress("scope-stale-error", "req-stale-error")
    assert state is not None
    assert "error" not in state
    assert "error_class" not in state
    assert "failure_kind" not in state
    events = state.get("diagnostic_events")
    assert isinstance(events, list)
    assert events[0].get("error") == "Provider unavailable"
    assert "error" not in events[-1]


def test_error_update_clears_stale_success(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=4700.0)
    von_routes._set_tool_progress(
        "scope-stale-success",
        "req-stale-success",
        {
            "status": "llm_call_end",
            "stage": "model_call",
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
            "stage": "model_call",
            "request_id": "req-stale-success",
            "error": "Model request failed",
        },
    )

    state = von_routes._get_tool_progress(
        "scope-stale-success",
        "req-stale-success",
    )
    assert state is not None
    assert state["status"] == "error"
    assert state.get("success") is not True
    assert state.get("error") == "Model request failed"


def test_tool_summary_survives_a_long_heartbeat_tail(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=5200.0)
    von_routes._set_tool_progress(
        "scope-heartbeat-tail",
        "req-heartbeat-tail",
        {
            "status": "tool_call_start",
            "phase": "tool_execute",
            "tool": "read_evidence",
            "batch_size": 1,
            "call_id": "call-read-evidence",
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
            "tool": "read_evidence",
            "batch_size": 1,
            "call_id": "call-read-evidence",
            "result_summary": "One evidence item read.",
            "request_id": "req-heartbeat-tail",
        },
    )
    for _ in range(von_routes._TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT + 5):
        clock["now"] += float(von_routes._TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC)
        von_routes._set_tool_progress(
            "scope-heartbeat-tail",
            "req-heartbeat-tail",
            {"status": "heartbeat", "request_id": "req-heartbeat-tail"},
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
    tool_history = snapshot.get("tool_history")
    assert isinstance(tool_history, list)
    assert tool_history[0]["tool"] == "read_evidence"
    assert tool_history[0]["resultSummary"] == "One evidence item read."


def test_relation_start_and_end_collapse_to_one_human_tool_row(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=5600.0)
    scope_key = "scope-semantic-relation"
    request_id = "req-semantic-relation"
    call_id = "call-semantic-relation"
    running_summary = (
        "Add Relationship: Subject: Nathan Young Doctoral Candidature Situation; "
        "Relation: Has Doctoral Supervisor; Object: Robert Amor (in progress)."
    )
    terminal_summary = (
        "Add Relationship reported that no change was needed: "
        "Subject: Nathan Young Doctoral Candidature Situation; "
        "Relation: Has Doctoral Supervisor; Object: Robert Amor."
    )
    running_operation = _relation_semantic_operation(lifecycle_status="running")
    terminal_operation = _relation_semantic_operation(
        lifecycle_status="succeeded",
        success=True,
        result={"effect_status": "succeeded", "changed": False},
    )

    von_routes._set_tool_progress(
        scope_key,
        request_id,
        {
            "status": "tool_call_start",
            "event_kind": "tool_call_start",
            "phase": "adaptive_research",
            "tool": "add_relationship",
            "call_id": call_id,
            "result_summary": running_summary,
            "semantic_operation": running_operation,
            "request_id": request_id,
        },
    )
    clock["now"] += 0.1
    von_routes._set_tool_progress(
        scope_key,
        request_id,
        {
            "status": "tool_completed",
            "event_kind": "tool_call_end",
            "phase": "adaptive_research",
            "tool": "add_relationship",
            "call_id": call_id,
            "success": True,
            "result_summary": terminal_summary,
            "semantic_operation": terminal_operation,
            "request_id": request_id,
        },
    )

    snapshot = von_routes._snapshot_tool_progress_for_request(
        scope_key,
        request_id,
    )
    assert snapshot is not None
    assert snapshot["result_summary"] == terminal_summary
    assert snapshot["tool_call_count"] == 1
    assert snapshot["tool_success_count"] == 1
    assert snapshot["tool_pending_count"] == 0
    assert snapshot["semantic_operation"] == terminal_operation
    semantic_events = [
        event["semantic_operation"]
        for event in snapshot["diagnostic_events"]
        if "semantic_operation" in event
    ]
    assert semantic_events == [running_operation, terminal_operation]
    assert snapshot["tool_history"] == [
        {
            "tool": "add_relationship",
            "workflowTask": "",
            "batchSize": None,
            "phase": "adaptive_research",
            "resultSummary": terminal_summary,
            "success": True,
            "callId": call_id,
            "semanticOperation": terminal_operation,
        }
    ]

    diagnostics = von_routes._build_turn_execution_diagnostics(
        request_id=request_id,
        prompt_text="Reassert the represented supervision relation.",
        tool_progress_state=snapshot,
    )
    assert diagnostics["latest_progress"]["semantic_operation"] == terminal_operation
    assert diagnostics["latest_progress"]["diagnostic_events"][-1][
        "semantic_operation"
    ] == terminal_operation
    assert diagnostics["tool_history"][0]["semanticOperation"] == terminal_operation


def test_serialisation_exposes_a_bounded_timing_trace(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=6000.0)
    von_routes._set_tool_progress(
        "scope-timing",
        "req-timing",
        {
            "status": "thinking",
            "stage": "tool_execute",
            "phase": "tool_execute",
            "request_id": "req-timing",
        },
    )
    clock["now"] += 1.0
    von_routes._set_tool_progress(
        "scope-timing",
        "req-timing",
        {
            "status": "heartbeat",
            "stage": "response_finalising",
            "phase": "response_finalising",
            "timing_spans": [
                {
                    "span_id": "finalise-response",
                    "stage_id": "response_finalising",
                    "operation_kind": "diagnostics_assembly",
                    "operation_name": "build_turn_execution_diagnostics",
                    "duration_ms": 37,
                    "status": "success",
                }
            ],
        },
    )

    state = von_routes._get_tool_progress("scope-timing", "req-timing")
    assert state is not None
    assert "_timing_spans" in state
    serialised = von_routes._serialise_tool_progress_state(
        state,
        now_epoch=clock["now"],
    )
    trace = serialised["turn_timing_trace"]
    assert trace["schema_version"] == "turn_timing_trace.v1"
    assert "timing_spans" in serialised
    assert "_timing_spans" not in serialised
    assert any(
        span.get("span_id") == "finalise-response" for span in trace["spans"]
    )
    assert serialised["timing_summary"]["slowest_spans"]


def test_diagnostics_include_neutral_model_tool_and_support_timing() -> None:
    diagnostics = von_routes._build_turn_execution_diagnostics(
        request_id="req-diagnostics-timing",
        prompt_text="What happened?",
        tool_progress_state={"request_id": "req-diagnostics-timing"},
        llm_calls=[
            {
                "stage": "model_call",
                "model": "test-model",
                "provider": "openai",
                "prompt_id": "#V#adaptive_turn_support_prompt",
                "duration_ms": 250,
                "success": True,
            }
        ],
        tool_invocations=[
            {
                "tool": "read_evidence",
                "duration_ms": 19,
                "status": "completed",
                "arguments": {"handle": "ev_123"},
            }
        ],
        timing_spans=[
            {
                "span_id": "persist-history",
                "stage_id": "response_finalising",
                "operation_kind": "chat_history_persistence",
                "operation_name": "persist_assistant_message",
                "duration_ms": 41,
            }
        ],
    )

    trace = diagnostics["turn_timing_trace"]
    timing_breakdown = diagnostics["timing_breakdown"]
    assert trace["summary"]["llm_elapsed_ms"] == 250
    assert trace["summary"]["tool_elapsed_ms"] == 19
    assert trace["model_prompt_summary"][0]["prompt_id"] == (
        "#V#adaptive_turn_support_prompt"
    )
    assert any(
        row["operation_kind"] == "chat_history_persistence"
        for row in timing_breakdown["operation_totals"]
    )
    assert timing_breakdown["slowest_spans"][0]["duration_ms"] == 250


def test_response_finalising_eta_is_bounded() -> None:
    baseline = von_routes._estimate_response_finalising_eta_ms(
        response_text=None,
        tool_message_count=0,
        persist_history=False,
    )
    elevated = von_routes._estimate_response_finalising_eta_ms(
        response_text="x" * 20_000,
        tool_message_count=400,
        persist_history=True,
    )
    assert 800 <= baseline <= 12_000
    assert 800 <= elevated <= 12_000
    assert elevated >= baseline
