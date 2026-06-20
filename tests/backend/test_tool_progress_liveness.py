"""Tests for generate() tool-progress liveness telemetry (JVNAUTOSCI-1103)."""

import os

from flask import Flask

import src.backend.server.routes.von_routes as von_routes
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


def test_selected_workflow_execution_events_survive_finalising_progress(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=1000.0)

    von_routes._set_tool_progress(
        "scope-workflow",
        "req-workflow",
        {
            "status": "workflow_execution_start",
            "stage": "selected_workflow_execution",
            "phase": "selected_workflow_execution",
            "selected_workflow_id": "#V#example_workflow",
            "selected_workflow_execution_event": {
                "status": "workflow_execution_start",
                "event_kind": "workflow_execution_start",
                "workflow_id": "#V#example_workflow",
                "selected_workflow_id": "#V#example_workflow",
            },
        },
    )
    clock["now"] += 1.0
    von_routes._set_tool_progress(
        "scope-workflow",
        "req-workflow",
        {
            "status": "workflow_step_complete",
            "stage": "selected_workflow_execution",
            "phase": "selected_workflow_execution",
            "selected_workflow_id": "#V#example_workflow",
            "selected_workflow_execution_event": {
                "status": "workflow_step_complete",
                "event_kind": "workflow_step_complete",
                "workflow_id": "#V#example_workflow",
                "selected_workflow_id": "#V#example_workflow",
                "state_id": "fetch_identity",
                "action_id": "fetch_concept",
                "action_status": "success",
                "action_outcome": "success",
                "duration_ms": 17,
            },
        },
    )
    clock["now"] += 1.0
    von_routes._set_tool_progress(
        "scope-workflow",
        "req-workflow",
        {
            "status": "heartbeat",
            "stage": "response_finalising",
            "phase": "response_finalising",
            "result_summary": "Assembling the final response payload.",
        },
    )

    state = von_routes._get_tool_progress("scope-workflow", "req-workflow")
    assert state is not None
    serialised = von_routes._serialise_tool_progress_state(state, now_epoch=clock["now"])

    selected_execution = serialised["selected_workflow_execution"]
    assert selected_execution["schema_version"] == "selected_workflow_execution.v1"
    assert selected_execution["selected_workflow_id"] == "#V#example_workflow"
    assert [event["status"] for event in selected_execution["events"]] == [
        "workflow_execution_start",
        "workflow_step_complete",
    ]
    assert selected_execution["latest_event"]["action_id"] == "fetch_concept"
    assert serialised["stage"] == "response_finalising"


def test_selected_workflow_execution_progress_facts_survive_serialisation(
    monkeypatch,
) -> None:
    clock = _set_clock(monkeypatch, start=1200.0)

    progress_facts = [
        {
            "schema_version": "workflow_progress_projection.v1",
            "fact_id": "email_subject",
            "label": "Email subject",
            "status": "available",
            "present": True,
            "redacted": False,
            "truncated": False,
            "value": "Research digest: arXiv attention paper",
            "value_kind": "title",
            "visibility": "default",
            "source_path": "context.email.subject",
            "resolved_path": "context.email.subject",
            "contract_id": "#V#email_subject_progress_fact",
        },
        {
            "schema_version": "workflow_progress_projection.v1",
            "fact_id": "paper_concept",
            "label": "Paper concept",
            "status": "available",
            "present": True,
            "redacted": False,
            "truncated": False,
            "value": "#V#paper_attention_is_all_you_need",
            "value_kind": "concept_id",
            "visibility": "expert",
            "source_path": "action_outputs.paper.concept_id",
        },
    ]

    von_routes._set_tool_progress(
        "scope-workflow-facts",
        "req-workflow-facts",
        {
            "status": "workflow_step_complete",
            "stage": "selected_workflow_execution",
            "phase": "selected_workflow_execution",
            "selected_workflow_id": "#V#research_triage_workflow",
            "selected_workflow_execution_event": {
                "status": "workflow_step_complete",
                "event_kind": "workflow_step_complete",
                "workflow_id": "#V#research_triage_workflow",
                "selected_workflow_id": "#V#research_triage_workflow",
                "state_id": "read_message",
                "action_id": "gmail_read",
                "progress_facts": progress_facts,
            },
        },
    )
    clock["now"] += 1.0
    von_routes._set_tool_progress(
        "scope-workflow-facts",
        "req-workflow-facts",
        {
            "status": "heartbeat",
            "stage": "response_finalising",
            "phase": "response_finalising",
        },
    )

    state = von_routes._get_tool_progress(
        "scope-workflow-facts",
        "req-workflow-facts",
    )
    assert state is not None
    serialised = von_routes._serialise_tool_progress_state(state, now_epoch=clock["now"])

    selected_execution = serialised["selected_workflow_execution"]
    latest_event = selected_execution["latest_event"]
    assert latest_event["progress_facts"][0]["label"] == "Email subject"
    assert latest_event["progress_facts"][0]["value"] == (
        "Research digest: arXiv attention paper"
    )
    assert latest_event["progress_facts"][1]["visibility"] == "expert"
    assert serialised["progress_facts"][0]["fact_id"] == "email_subject"
    heartbeat = serialised["diagnostic_events"][-1]
    assert heartbeat["status"] == "heartbeat"
    assert "progress_facts" not in heartbeat


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
    flush_queued_tool_progress_states(force=True)

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


def test_tool_progress_batches_non_terminal_updates_until_flush(
    monkeypatch,
) -> None:
    writes: list[dict] = []

    def _fake_store(*, scope_key, request_id, payload, ttl_seconds):
        writes.append(
            {
                "scope_key": scope_key,
                "request_id": request_id,
                "payload": dict(payload),
                "ttl_seconds": ttl_seconds,
            }
        )
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
            "phase": "workflow_dispatch",
            "request_id": "req-batched",
        },
    )
    von_routes._set_tool_progress(
        "scope-batched",
        "req-batched",
        {
            "status": "heartbeat",
            "request_id": "req-batched",
        },
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
    assert writes[0]["payload"]["sequence_no"] == 2
    assert writes[0]["payload"]["progress_persistence"]["status"] == "flushed"


def test_tool_progress_terminal_state_flushes_immediately(monkeypatch) -> None:
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


def test_tool_progress_reports_failed_flush_and_keeps_pending(monkeypatch) -> None:
    def _fake_store(*, scope_key, request_id, payload, ttl_seconds):
        return False

    monkeypatch.setattr(
        "src.backend.services.tool_progress_store_service.store_tool_progress_state",
        _fake_store,
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


def test_tool_progress_synchronous_durability_opt_in_flushes(monkeypatch) -> None:
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
    flush_queued_tool_progress_states(force=True)

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


def test_progress_endpoint_uses_header_user_scope_without_session_resolution(
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
        lambda: (_ for _ in ()).throw(
            AssertionError("progress route should not require session auth lookup")
        ),
    )

    request_id = "req-header-user-scope"
    von_routes._set_tool_progress(
        "user:#V#test_user",
        request_id,
        {
            "status": "thinking",
            "phase": "workflow_dispatch",
            "phase_label": "Selecting workflow",
            "request_id": request_id,
        },
    )

    client = app.test_client()
    response = client.get(
        f"/von/progress/{request_id}",
        headers={"X-User-Concept-ID": "#V#test_user"},
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("request_id") == request_id
    assert body.get("stage") == "workflow_dispatch"
    assert body.get("phase_label") == "Selecting workflow"


def test_progress_endpoint_uses_window_header_scope_without_session_resolution(
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
        lambda: (_ for _ in ()).throw(
            AssertionError("progress route should not require session auth lookup")
        ),
    )

    request_id = "req-header-window-scope"
    window_session_id = "ws_header_only_progress"
    von_routes._set_tool_progress(
        f"anon:window:{window_session_id}",
        request_id,
        {
            "status": "thinking",
            "phase": "context_build",
            "phase_label": "Building context",
            "request_id": request_id,
        },
    )

    client = app.test_client()
    response = client.get(
        f"/von/progress/{request_id}",
        headers={
            "X-Von-Window-Session": window_session_id,
            "X-User-Concept-ID": "#V#test_user",
        },
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("request_id") == request_id
    assert body.get("stage") == "context_build"
    assert body.get("phase_label") == "Building context"


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
    flush_queued_tool_progress_states(force=True)

    with von_routes._TOOL_PROGRESS_LOCK:
        von_routes._TOOL_PROGRESS.clear()

    response = client.get("/von/progress/req-legacy-scope")
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("request_id") == "req-legacy-scope"
    assert body.get("stage") == "context_build"
    assert body.get("phase_label") == "Building context"


def test_progress_endpoint_recovers_session_scoped_progress_after_auth_scope_shift(
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
        lambda: "#V#test_user",
    )

    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["tool_progress_scope"] = "legacy_cookie_scope"

    von_routes._set_tool_progress(
        "anon:session:legacy_cookie_scope",
        "req-auth-shift",
        {
            "status": "thinking",
            "phase": "workflow_dispatch",
            "phase_label": "Selecting workflow",
            "request_id": "req-auth-shift",
        },
    )
    flush_queued_tool_progress_states(force=True)

    with von_routes._TOOL_PROGRESS_LOCK:
        von_routes._TOOL_PROGRESS.clear()

    response = client.get("/von/progress/req-auth-shift")
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("request_id") == "req-auth-shift"
    assert body.get("stage") == "workflow_dispatch"
    assert body.get("phase_label") == "Selecting workflow"
    assert body.get("resolved_scope_key") == "anon:session:legacy_cookie_scope"
    assert body.get("progress_source") == "alternate_scope_fallback"


def test_progress_endpoint_recovers_window_scoped_progress_after_auth_scope_shift(
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
        lambda: "#V#test_user",
    )

    window_session_id = "ws_progress_auth_scope_shift"
    von_routes._set_tool_progress(
        f"anon:window:{window_session_id}",
        "req-window-auth-shift",
        {
            "status": "thinking",
            "phase": "workflow_dispatch_prepare",
            "phase_label": "Preparing workflow dispatch",
            "request_id": "req-window-auth-shift",
        },
    )
    flush_queued_tool_progress_states(force=True)

    with von_routes._TOOL_PROGRESS_LOCK:
        von_routes._TOOL_PROGRESS.clear()

    client = app.test_client()
    response = client.get(
        "/von/progress/req-window-auth-shift",
        headers={
            "X-Von-Window-Session": window_session_id,
            "X-User-Concept-ID": "#V#test_user",
        },
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("request_id") == "req-window-auth-shift"
    assert body.get("stage") == "workflow_dispatch_prepare"
    assert body.get("phase_label") == "Preparing workflow dispatch"
    assert body.get("resolved_scope_key") == f"anon:window:{window_session_id}"
    assert body.get("progress_source") == "alternate_scope_fallback"


def test_progress_endpoint_prefers_live_alternate_scope_before_persisted_miss(
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
        lambda: "#V#test_user",
    )

    window_session_id = "ws_progress_memory_first"
    request_id = "req-memory-first"
    von_routes._set_tool_progress(
        f"anon:window:{window_session_id}",
        request_id,
        {
            "status": "thinking",
            "phase": "workflow_dispatch_prepare",
            "phase_label": "Preparing workflow dispatch",
            "request_id": request_id,
        },
    )

    def _unexpected_fetch(*, scope_key: str, request_id: str):
        raise AssertionError(
            f"persisted fetch should not run before checking live alternate scope "
            f"(scope_key={scope_key}, request_id={request_id})"
        )

    monkeypatch.setattr(von_routes, "fetch_tool_progress_state", _unexpected_fetch)

    client = app.test_client()
    response = client.get(
        f"/von/progress/{request_id}",
        headers={
            "X-Von-Window-Session": window_session_id,
            "X-User-Concept-ID": "#V#test_user",
        },
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("request_id") == request_id
    assert body.get("stage") == "workflow_dispatch_prepare"
    assert body.get("phase_label") == "Preparing workflow dispatch"
    assert body.get("resolved_scope_key") == f"anon:window:{window_session_id}"
    assert body.get("progress_source") == "alternate_scope_fallback"


def test_progress_endpoint_prefers_freshest_alternate_scope_payload(
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
        lambda: "#V#test_user",
    )

    clock = _set_clock(monkeypatch, start=4000.0)
    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["tool_progress_scope"] = "legacy_cookie_scope"

    von_routes._set_tool_progress(
        "user:#V#test_user",
        "req-freshest-scope",
        {
            "status": "thinking",
            "phase": "context_build",
            "phase_label": "Building context",
            "request_id": "req-freshest-scope",
        },
    )

    clock["now"] += 1.0
    von_routes._set_tool_progress(
        "anon:session:legacy_cookie_scope",
        "req-freshest-scope",
        {
            "status": "thinking",
            "phase": "workflow_discovery",
            "phase_label": "Searching for workflows",
            "request_id": "req-freshest-scope",
        },
    )

    response = client.get("/von/progress/req-freshest-scope")
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("request_id") == "req-freshest-scope"
    assert body.get("stage") == "workflow_discovery"
    assert body.get("phase_label") == "Searching for workflows"
    assert body.get("resolved_scope_key") == "anon:session:legacy_cookie_scope"
    assert body.get("progress_source") == "alternate_scope_fallback"


def test_register_tool_progress_scope_aliases_copies_current_state_to_all_request_scopes(
    monkeypatch,
) -> None:
    _set_clock(monkeypatch, start=5000.0)

    mirror_scope_keys: list[str] = []
    primary_scope_key = "anon:window:ws_scope_alias"
    request_id = "req-scope-alias"

    von_routes._set_tool_progress(
        primary_scope_key,
        request_id,
        {
            "status": "thinking",
            "phase": "context_build",
            "phase_label": "Building context",
            "request_id": request_id,
        },
    )

    returned_scope_keys = von_routes._register_tool_progress_scope_aliases(
        request_id=request_id,
        primary_scope_key=primary_scope_key,
        mirror_scope_keys=mirror_scope_keys,
        user_concept_id="#V#test_user",
        window_session_id="ws_scope_alias",
        anonymous_session_id="legacy_cookie_scope",
    )

    assert returned_scope_keys == [
        "user:#V#test_user",
        "anon:session:legacy_cookie_scope",
    ]

    user_scope_state = von_routes._get_tool_progress("user:#V#test_user", request_id)
    assert user_scope_state is not None
    assert user_scope_state.get("stage") == "context_build"

    session_scope_state = von_routes._get_tool_progress(
        "anon:session:legacy_cookie_scope", request_id
    )
    assert session_scope_state is not None
    assert session_scope_state.get("stage") == "context_build"


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
    serialised = von_routes._serialise_tool_progress_state(
        state, now_epoch=clock["now"]
    )

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
    serialised = von_routes._serialise_tool_progress_state(
        state, now_epoch=clock["now"]
    )

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
            if isinstance(entry, dict) and entry.get("status") == "llm_request_prepared"
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

    serialised = von_routes._serialise_tool_progress_state(
        state, now_epoch=clock["now"]
    )
    assert (
        serialised["goal_label"]
        == "Fully represent the paper #V#uploaded_file_copy_123"
    )

    events = serialised.get("diagnostic_events")
    assert isinstance(events, list)
    assert events[-1].get("goal_label") == serialised["goal_label"]
    assert events[-1].get("workflow_candidate_count") == 1

    progress_events = von_routes._normalise_progress_events_from_diagnostic_events(
        events
    )
    assert progress_events[-1].get("goal_label") == serialised["goal_label"]

    summary = von_routes._build_tool_progress_compact_summary(serialised)
    assert isinstance(summary, dict)
    assert summary.get("goal_label") == serialised["goal_label"]


def test_selector_candidate_progress_fields_are_serialised(monkeypatch) -> None:
    clock = _set_clock(monkeypatch, start=1_800_000_000.0)

    von_routes._set_tool_progress(
        "scope-selector",
        "req-selector",
        {
            "status": "thinking",
            "phase": "selector_preparation",
            "request_id": "req-selector",
            "selector_candidate_ids": [
                "#V#zhan_gmail_arxiv_ingestion_workflow",
                "",
                "#V#tool_calling_workflow",
            ],
            "selector_discovered_workflow_ids": [
                "#V#zhan_gmail_arxiv_ingestion_workflow"
            ],
            "selector_excluded_candidate_ids": ["#V#excluded_workflow"],
            "selector_candidate_count": 2,
            "selector_excluded_candidate_count": 1,
        },
    )

    state = von_routes._get_tool_progress("scope-selector", "req-selector")
    assert state is not None

    serialised = von_routes._serialise_tool_progress_state(
        state, now_epoch=clock["now"]
    )
    events = serialised.get("diagnostic_events")
    assert isinstance(events, list)
    assert events[-1].get("selector_candidate_ids") == [
        "#V#zhan_gmail_arxiv_ingestion_workflow",
        "#V#tool_calling_workflow",
    ]
    assert events[-1].get("selector_discovered_workflow_ids") == [
        "#V#zhan_gmail_arxiv_ingestion_workflow"
    ]
    assert events[-1].get("selector_excluded_candidate_ids") == [
        "#V#excluded_workflow"
    ]
    assert events[-1].get("selector_candidate_count") == 2
    assert events[-1].get("selector_excluded_candidate_count") == 1


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


def test_serialised_progress_includes_interpretability_payload_for_workflow_tool_and_chat() -> (
    None
):
    workflow_snapshot = von_routes._serialise_tool_progress_state(
        {
            "request_id": "req-interpret-workflow",
            "status": "thinking",
            "phase": "workflow_dispatch",
            "stage": "workflow_dispatch",
            "stage_label": "Selecting workflow",
            "selected_workflow_id": "#V#mail_identity_lookup_workflow",
            "selected_workflow_name": "Mail identity lookup workflow",
            "workflow_stage_path": {
                "schema_version": "conversation_turn_stage_path.v1",
                "path": [
                    {
                        "stage_id": "workflow_dispatch_prepare",
                        "stage_label": "Workflow dispatch preparation",
                    },
                    {"stage_id": "workflow_dispatch", "stage_label": "Selecting workflow"},
                ],
            },
        }
    )
    workflow_interpretability = workflow_snapshot.get("thinking_interpretability")
    assert isinstance(workflow_interpretability, dict)
    assert workflow_interpretability.get("execution_family") == "selected_workflow"
    assert (
        workflow_interpretability.get("identity_summary")
        == "Selected workflow: Mail identity lookup workflow (#V#mail_identity_lookup_workflow)"
    )

    tool_snapshot = von_routes._serialise_tool_progress_state(
        {
            "request_id": "req-interpret-tool",
            "status": "thinking",
            "phase": "tool_execute",
            "stage": "tool_execute",
            "stage_label": "Applying actions",
            "result_summary": "Running tool pipeline.",
            "tool_history": [
                {"tool": "fetch_concept"},
                {"tool": "task_get"},
            ],
        }
    )
    tool_interpretability = tool_snapshot.get("thinking_interpretability")
    assert isinstance(tool_interpretability, dict)
    assert tool_interpretability.get("execution_family") == "tool_orchestration"
    assert "General tool use" in str(tool_interpretability.get("identity_summary"))

    chat_snapshot = von_routes._serialise_tool_progress_state(
        {
            "request_id": "req-interpret-chat",
            "status": "thinking",
            "phase": "context_build",
            "stage": "context_build",
            "phase_label": "Understanding request",
            "result_summary": "Preparing direct chat response.",
        }
    )
    chat_interpretability = chat_snapshot.get("thinking_interpretability")
    assert isinstance(chat_interpretability, dict)
    assert chat_interpretability.get("execution_family") == "chat_response"
    assert chat_interpretability.get("identity_summary") == "Direct chat response"


def test_workflow_discovery_progress_payload_preserves_explicit_no_match_state() -> (
    None
):
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


def test_turn_execution_diagnostics_rebuilds_phase_and_tool_history(
    monkeypatch,
) -> None:
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
    assert (
        workflow_stage_model.get("schema_version") == "conversation_turn_stage_model.v1"
    )

    workflow_stage_path = diagnostics.get("workflow_stage_path")
    assert isinstance(workflow_stage_path, dict)
    assert (
        workflow_stage_path.get("schema_version") == "conversation_turn_stage_path.v1"
    )
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


def test_turn_execution_timing_uses_model_name_and_duration_baseline(
    monkeypatch,
) -> None:
    diagnostics = von_routes._build_turn_execution_diagnostics(
        request_id="req-duration-baseline",
        prompt_text="Run a workflow step",
        llm_calls=[
            {
                "type": "llm.generate",
                "stage": "selector_decision",
                "workflow_stage_id": "#V#selector_step",
                "model_name": "gpt-baseline",
                "duration_ms": 420.4,
                "provider": "openai",
                "historical_observation_count": 5,
                "historical_mean_duration_ms": 250.0,
                "historical_stddev_duration_ms": 40.0,
                "duration_deviation_classification": "slower_than_usual",
            }
        ],
    )

    timing = diagnostics.get("timing_breakdown")
    assert isinstance(timing, dict)
    llm_rows = timing.get("llm_calls_by_stage_model")
    assert isinstance(llm_rows, list)
    assert len(llm_rows) == 1
    row = llm_rows[0]
    assert row.get("stage") == "#V#selector_step"
    assert row.get("model") == "gpt-baseline"
    assert row.get("historical_observation_count") == 5
    assert row.get("historical_mean_duration_ms") == 250.0
    assert row.get("duration_deviation_classification") == "slower_than_usual"


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


def test_llm_exchange_identity_survives_progress_event_serialisation(monkeypatch) -> None:
    _set_clock(monkeypatch, start=5600.0)

    von_routes._set_tool_progress(
        "scope-llm-exchange",
        "req-llm-exchange",
        {
            "status": "llm_call_end",
            "stage": "selected_workflow_execution",
            "workflow_stage_id": "selected_workflow_execution",
            "request_id": "req-llm-exchange",
            "call_id": "llm-abc123:attempt:1",
            "llm_exchange_id": "llm-abc123",
            "llm_request_state": "completed",
            "llm_request": {
                "prompt": {
                    "text": "PROMPT-FOR-STABLE-LLM-EXCHANGE",
                    "char_count": 30,
                }
            },
            "llm_response_preview": {
                "text": "RESPONSE-FOR-STABLE-LLM-EXCHANGE",
                "char_count": 32,
            },
            "model": "gpt-oss:20b",
            "provider": "ollama",
            "duration_ms": 2671,
            "success": True,
        },
    )

    snapshot = von_routes._get_tool_progress("scope-llm-exchange", "req-llm-exchange")
    assert snapshot is not None
    events = snapshot.get("diagnostic_events")
    assert isinstance(events, list)
    assert events
    event = events[-1]
    assert event["call_id"] == "llm-abc123:attempt:1"
    assert event["llm_exchange_id"] == "llm-abc123"
    assert event["llm_request_state"] == "completed"
    assert event["llm_request"]["prompt"]["text"] == "PROMPT-FOR-STABLE-LLM-EXCHANGE"
    assert event["llm_response_preview"]["text"] == "RESPONSE-FOR-STABLE-LLM-EXCHANGE"


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


def test_recovery_progress_projection_survives_finalising_tail(
    monkeypatch,
) -> None:
    clock = _set_clock(monkeypatch, start=5325.0)

    von_routes._set_tool_progress(
        "scope-recovery-tail",
        "req-recovery-tail",
        {
            "status": "tool_failed",
            "phase": "tool_execute",
            "phase_label": "Executing tools",
            "request_id": "req-recovery-tail",
            "tool": "gmail_list_messages",
            "error": "gmail_list_messages failed: invalid_grant",
            "error_code": "invalid_grant",
            "error_class": "OAuthError",
            "failure_kind": "tool_auth",
            "success": False,
        },
    )
    clock["now"] += 0.2
    recovery_prompt_context_diagnostics = {
        "schema_version": "llm_prompt_context_diagnostics.v1",
        "workflow_state_id": "recovery_decision",
        "rendered_prompt_chars": 150_704,
        "recovery_context_compaction": {
            "schema_version": "recovery_context_compaction.v1",
            "enabled": True,
            "workflow_state_id": "recovery_decision",
            "rendered_context_chars": 11_920,
            "rendered_context_field_count": 5,
            "total_context_budget_exhausted": True,
        },
    }
    von_routes._set_tool_progress(
        "scope-recovery-tail",
        "req-recovery-tail",
        {
            "status": "phase_transition",
            "phase": "recovery_decision",
            "phase_label": "Recovery decision",
            "workflow_stage_id": "recovery_decision",
            "request_id": "req-recovery-tail",
            "result_summary": "Choosing recovery path after tool failure.",
            "completion_gate_loop_attempts": 4,
        },
    )
    clock["now"] += 0.2
    von_routes._set_tool_progress(
        "scope-recovery-tail",
        "req-recovery-tail",
        {
            "status": "llm_call_end",
            "phase": "recovery_decision",
            "phase_label": "Recovery decision",
            "workflow_stage_id": "recovery_decision",
            "request_id": "req-recovery-tail",
            "model": "qwen3:8b",
            "provider": "ollama",
            "duration_ms": 931_385,
            "prompt_context_diagnostics": recovery_prompt_context_diagnostics,
            "llm_request": {
                "prompt": {
                    "text": "RECOVERY PROMPT PREVIEW",
                    "char_count": 150_704,
                    "is_truncated": True,
                }
            },
            "llm_response_preview": {
                "text": '{"decision":"answer_with_recovery_context"}',
                "char_count": 43,
            },
            "llm_request_state": "completed",
        },
    )
    clock["now"] += 0.2
    von_routes._set_tool_progress(
        "scope-recovery-tail",
        "req-recovery-tail",
        {
            "status": "heartbeat",
            "phase": "response_finalising",
            "phase_label": "Finalising response",
            "request_id": "req-recovery-tail",
            "result_summary": "Assembling the final response payload.",
        },
    )

    snapshot = von_routes._snapshot_tool_progress_for_request(
        "scope-recovery-tail",
        "req-recovery-tail",
    )
    assert snapshot is not None
    assert snapshot["stage"] == "response_finalising"

    workflow_stage_path = snapshot.get("workflow_stage_path")
    assert isinstance(workflow_stage_path, dict)
    assert [
        entry.get("stage_id")
        for entry in workflow_stage_path.get("path", [])
        if isinstance(entry, dict) and entry.get("stage_id")
    ] == [
        "recovery_decision",
        "response_finalising",
    ]

    recovery_progress = snapshot.get("recovery_progress")
    assert isinstance(recovery_progress, dict)
    assert recovery_progress["schema_version"] == "thinking_recovery_progress.v1"
    assert recovery_progress["active"] is False
    assert recovery_progress["recent"] is True
    assert recovery_progress["finalising_after_recovery"] is True
    assert recovery_progress["current_stage_id"] == "response_finalising"
    assert recovery_progress["latest_recovery_stage_id"] == "recovery_decision"
    assert recovery_progress["attempt_count"] == 4
    assert recovery_progress["latest_failure"]["tool"] == "gmail_list_messages"
    assert recovery_progress["latest_failure"]["error_code"] == "invalid_grant"
    assert (
        recovery_progress["prompt_context_diagnostics"]["rendered_prompt_chars"]
        == 150_704
    )
    assert recovery_progress["large_llm_call_alerts"][0]["stage"] == (
        "recovery_decision"
    )
    assert recovery_progress["large_llm_call_alerts"][0]["prompt_char_count"] == (
        150_704
    )

    interpretation = snapshot.get("thinking_interpretability")
    assert isinstance(interpretation, dict)
    assert interpretation["progress_kind"] == "post_recovery_finalising"
    assert "Recovery decision" in interpretation["step_summary"]
    assert interpretation["recovery_progress"]["latest_failure"]["error_code"] == (
        "invalid_grant"
    )


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


def test_terminal_progress_payload_prefers_persisted_completion_gate() -> None:
    payload = von_routes._build_terminal_tool_progress_payload(
        request_id="req-persisted-gate",
        aux_calls=[],
        completion_gate={
            "decision": "escalation_required",
            "decision_reason": "Workflow dispatch failed before execution.",
            "safe_to_claim_completion": False,
            "requires_follow_up": True,
            "blocking_effect_ids": ["effect_workflow_execution_1"],
        },
    )

    assert payload["status"] == "follow_up_required"
    assert payload["success"] is False
    assert payload["completion_gate_requires_follow_up"] is True
    assert payload["completion_gate_safe_to_claim_completion"] is False
    assert payload["completion_gate_decision"] == "escalation_required"
    assert payload["completion_gate_blocking_effect_ids"] == [
        "effect_workflow_execution_1"
    ]


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
    assert (
        von_routes._default_stage_label("response_finalising") == "Finalising response"
    )
    assert von_routes._default_stage_label("recovery_decision") == "Recovery decision"
    assert (
        von_routes._default_stage_label("apply_recovery_tool_batch")
        == "Executing recovery tools"
    )


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


def test_stage_diagnostics_preserve_task_details_from_live_stage_summaries() -> None:
    serialised = von_routes._serialise_tool_progress_state(
        {
            "request_id": "req-stage-summary-detail",
            "status": "heartbeat",
            "phase": "response_finalising",
            "stage": "response_finalising",
            "phase_label": "Finalising response",
            "updated_at_epoch": 5600.0,
            "last_activity_epoch": 5600.0,
            "last_stage_activity_epoch": 5600.0,
            "request_started_epoch": 5590.0,
            "_workflow_runtime_stages": [
                "workflow_dispatch_prepare",
                "tool_plan",
                "response_finalising",
            ],
            "_stage_summaries": {
                "workflow_dispatch_prepare": {
                    "stage_label": "Workflow dispatch preparation",
                    "event_count": 3,
                    "latest_status": "thinking",
                    "latest_result_summary": "Preparing workflow dispatch",
                    "latest_subtask": "Load workflow launch contract",
                },
                "tool_plan": {
                    "stage_label": "Tool-call planning",
                    "event_count": 2,
                    "latest_status": "thinking",
                    "latest_workflow_task": "fetch_concept",
                    "latest_tool": "task_get",
                },
            },
            "diagnostic_events": [],
        },
        now_epoch=5600.0,
    )

    stage_diagnostics = serialised.get("stage_diagnostics")
    assert isinstance(stage_diagnostics, list)
    by_stage = {
        entry.get("stage_id"): entry
        for entry in stage_diagnostics
        if isinstance(entry, dict) and isinstance(entry.get("stage_id"), str)
    }

    assert by_stage["workflow_dispatch_prepare"]["latest_subtask"] == (
        "Load workflow launch contract"
    )
    assert by_stage["tool_plan"]["latest_workflow_task"] == "fetch_concept"
    assert by_stage["tool_plan"]["latest_tool"] == "task_get"


def test_turn_execution_diagnostics_include_routing_diagnostics_from_selector_and_dispatch() -> (
    None
):
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


def test_turn_execution_diagnostics_include_stage_specific_user_utility_payloads() -> (
    None
):
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
                    "status": "success",
                    "source_path": "represented_screen_prompt",
                    "latency_ms": 44,
                    "model_id": "gpt-4.1-mini",
                    "input_summary": {
                        "needs_backfill": True,
                        "tool_message_count": 3,
                    },
                    "output_summary": {
                        "applied": True,
                        "presenter_format": (
                            "screen_backfill_from_represented_prompt_v1"
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
            "blocking_failure_codes": ["tool_execution_required_but_not_observed"],
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
    assert by_stage["workflow_dispatch_prepare"]["pre_dispatch"][
        "slowest_step_label"
    ] == ("Load workflow contract")
    assert by_stage["workflow_dispatch_prepare"]["latest_subtask"] == (
        "Resolve workflow inputs"
    )
    assert by_stage["tool_plan"]["tool_execution"]["planned_count"] == 2
    assert by_stage["screen_backfill"]["response_transformation"] == {
        "transform_name": "screen_backfill",
        "status": "success",
        "source_path": "represented_screen_prompt",
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
                "screen_backfill_from_represented_prompt_v1"
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
    assert (
        by_stage["postcondition_critic"]["critic_verdict"]["unresolved_check_count"]
        == 2
    )
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
        "stage": "selector_decision",
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
            "phase": "selector_preparation",
            "phase_label": "Preparing workflow selector",
            "request_id": "req-custom-workflow",
            "workflow_routing_aux": [
                {
                    **selector_prompt_entry,
                    "stage": "selector_preparation",
                }
            ],
            "counters": {"tools_started": 0, "tools_completed": 0},
        },
    )
    clock["now"] += 0.1
    von_routes._set_tool_progress(
        "scope-custom-workflow",
        "req-custom-workflow",
        {
            "status": "thinking",
            "phase": "selector_decision",
            "phase_label": "Workflow selector decision",
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
            "workflow_routing_aux": [
                {
                    **selector_prompt_entry,
                    "stage": "selector_preparation",
                },
                selector_success_entry,
            ],
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
    selector_preparation = next(
        (
            entry
            for entry in stage_diagnostics
            if isinstance(entry, dict)
            and entry.get("stage_id") == "selector_preparation"
        ),
        None,
    )
    assert selector_preparation is not None
    assert selector_preparation.get("llm_exchange_record_count") == 1
    assert selector_preparation.get("llm_exchange_entry_types") == [
        "workflow_selector_prompt"
    ]

    selector_decision = next(
        (
            entry
            for entry in stage_diagnostics
            if isinstance(entry, dict) and entry.get("stage_id") == "selector_decision"
        ),
        None,
    )
    assert selector_decision is not None
    assert selector_decision.get("llm_exchange_record_count") == 1
    assert selector_decision.get("llm_exchange_entry_types") == ["workflow_selector"]
    assert (
        selector_decision.get("latest_llm_exchange", {})
        .get("response_preview", {})
        .get("text")
        == selected_workflow_id
    )

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
    assert workflow_dispatch.get("llm_exchange_record_count") == 0
    assert workflow_dispatch.get("llm_exchange_entry_types") == []
    assert workflow_dispatch.get("latest_llm_exchange") is None

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
    assert (
        routing_diagnostics.get("dispatch", {}).get("dispatch_terminal_failure_reason")
        == "workflow_launch_input_resolution_failed"
    )
    assert (
        routing_diagnostics.get("dispatch", {}).get(
            "dispatch_terminal_failing_state_id"
        )
        == "prepare_spec"
    )
    assert (
        routing_diagnostics.get("dispatch", {}).get(
            "dispatch_terminal_failing_action_id"
        )
        == "tool.prepare_spec"
    )
    assert routing_diagnostics.get("dispatch", {}).get(
        "dispatch_terminal_unresolved_required_inputs"
    ) == ["invitation_text"]


def test_serialised_tool_progress_state_includes_live_workflow_routing_diagnostics() -> (
    None
):
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


def test_tool_progress_serialisation_exposes_bounded_turn_timing_trace(
    monkeypatch,
) -> None:
    clock = _set_clock(monkeypatch, start=3000.0)

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
                    "span_id": "support-finalise-debug",
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

    serialised = von_routes._serialise_tool_progress_state(state, now_epoch=clock["now"])
    trace = serialised["turn_timing_trace"]

    assert trace["schema_version"] == "turn_timing_trace.v1"
    assert "timing_spans" in serialised
    assert "_timing_spans" not in serialised
    assert any(
        span.get("span_id") == "support-finalise-debug" for span in trace["spans"]
    )
    assert serialised["timing_summary"]["slowest_spans"]


def test_turn_execution_diagnostics_include_model_prompt_and_tool_timing() -> None:
    diagnostics = von_routes._build_turn_execution_diagnostics(
        request_id="req-diagnostics-timing",
        prompt_text="What happened?",
        tool_progress_state={"request_id": "req-diagnostics-timing"},
        llm_calls=[
            {
                "workflow_stage_id": "workflow_routing",
                "model": "gpt-5-mini",
                "provider": "openai",
                "prompt_id": "#V#workflow_selector_prompt",
                "duration_ms": 250,
                "success": True,
            }
        ],
        tool_invocations=[
            {
                "tool": "turn_execution_get_live_progress",
                "duration_ms": 19,
                "status": "completed",
                "arguments": {"request_id": "req-diagnostics-timing"},
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
        "#V#workflow_selector_prompt"
    )
    assert any(
        row["operation_kind"] == "chat_history_persistence"
        for row in timing_breakdown["operation_totals"]
    )
    assert timing_breakdown["slowest_spans"][0]["duration_ms"] == 250
