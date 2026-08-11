from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from flask import Flask

from src.backend.services.adaptive_turn_service import AdaptiveTurnResult

_ADAPTIVE_RESPONSE = "A useful answer chosen by the adaptive turn."


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch) -> Flask:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_von_generate_adaptive_route")

    from src.backend.server.routes import von_routes
    from src.backend.workflows.durable import registry_factory

    adaptive_calls: list[dict[str, Any]] = []
    adaptive_state: dict[str, Any] = {
        "response_text": _ADAPTIVE_RESPONSE,
        "render_plan": None,
        "extra_messages": (),
        "tool_invocations": (),
        "terminal_status": "completed",
        "effect_finality_fallback": False,
    }

    def _execute_adaptive_turn(**kwargs: Any) -> AdaptiveTurnResult:
        adaptive_calls.append(dict(kwargs))
        return AdaptiveTurnResult(
            response_text=adaptive_state["response_text"],
            extra_messages=tuple(adaptive_state["extra_messages"]),
            tool_invocations=tuple(adaptive_state["tool_invocations"]),
            aux_llm_calls=(),
            llm_calls=(
                {
                    "type": "adaptive_turn_synthesis",
                    "model": "test-model",
                    "duration_ms": 1.0,
                },
            ),
            llm_usage={"total_tokens": 7},
            duration_ms=2.0,
            render_plan=adaptive_state["render_plan"],
            terminal_status=adaptive_state["terminal_status"],
            effect_finality_fallback=adaptive_state[
                "effect_finality_fallback"
            ],
        )

    def _retired_outer_controller_called(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError(
            "ordinary generation must not create, finalise, or semantically gate "
            "a master conversation-turn workflow"
        )

    monkeypatch.setattr(von_routes, "execute_adaptive_turn", _execute_adaptive_turn)
    monkeypatch.setattr(
        von_routes,
        "get_instance_manager",
        _retired_outer_controller_called,
    )
    monkeypatch.setattr(
        von_routes,
        "submit_verified_workflow_instance",
        _retired_outer_controller_called,
    )

    monkeypatch.setattr(registry_factory, "discover_workflow_ids", list)
    monkeypatch.setattr(
        registry_factory,
        "_launch_deferred_registry_work",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#michael_witbrock",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {
            "organisation_id": "#V#sail_lab",
            "chat_session_id": "session-adaptive-route-test",
            "role": "member",
        },
    )
    monkeypatch.setattr(von_routes, "get_llm_client", lambda **_kwargs: object())
    monkeypatch.setattr(
        von_routes,
        "get_model_registry_snapshot",
        lambda: {"source": "test", "models": []},
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_generate_requested_model",
        lambda *_args, **_kwargs: ("test-model", None, {}),
    )
    monkeypatch.setattr(
        von_routes,
        "get_show_tool_use_during_thinking",
        lambda: False,
    )
    monkeypatch.setattr(von_routes, "get_buttonify_model_enabled", lambda: False)
    monkeypatch.setattr(
        von_routes,
        "get_display_elements_screen_fence_compat_enabled",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        von_routes,
        "_add_chat_history_message",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_state",
        lambda **kwargs: {
            "session_id": kwargs["session_id"],
            "history": [],
        },
    )
    monkeypatch.setattr(
        von_routes,
        "build_context_concept_reference_metadata",
        lambda *_args, **_kwargs: {
            "source": "sent_context_user_assistant",
            "metadata_version": 1,
            "message_roles": ["user", "assistant"],
            "messages_scanned": 0,
            "concept_count": 0,
            "concept_count_capped": False,
            "max_concepts": None,
            "include_direct_supertypes": False,
            "max_direct_supertypes": 0,
            "concepts": [],
        },
    )
    monkeypatch.setattr(
        von_routes.PromptTemplateService,
        "resolve_prompt_text",
        lambda *_args, **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda *_args, **_kwargs: [],
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.config["TESTING"] = True
    flask_app.config["PROPAGATE_EXCEPTIONS"] = True
    flask_app.config["INTERNAL_MCP_ORCHESTRATOR"] = object()
    flask_app.config["INTERNAL_MCP_GATEWAY"] = None
    flask_app.config["CONTEXT"] = []
    flask_app.config["_ADAPTIVE_TURN_CALLS"] = adaptive_calls
    flask_app.config["_ADAPTIVE_TURN_STATE"] = adaptive_state
    flask_app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    return flask_app


def test_ordinary_generate_uses_adaptive_turn_without_master_workflow_or_gate(
    app: Flask,
) -> None:
    from src.backend.server.routes import von_routes

    assert not hasattr(von_routes, "_build_terminal_tool_progress_payload")
    assert not hasattr(von_routes, "strip_completion_ledger_suffix")

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Answer this ordinary scientific question."},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert isinstance(payload, dict)
    assert payload["response"] == _ADAPTIVE_RESPONSE

    adaptive_calls = app.config["_ADAPTIVE_TURN_CALLS"]
    assert len(adaptive_calls) == 1
    assert adaptive_calls[0]["prompt"] == "Answer this ordinary scientific question."
    assert adaptive_calls[0]["model_registry_snapshot"] == {
        "source": "test",
        "models": [],
    }

    llm_debug = payload["llm_debug"]
    assert llm_debug["llm_interaction"]["ordinary_turn_engine"] == (
        "direct_adaptive_turn"
    )
    assert llm_debug["llm_interaction"]["ordinary_turn_terminal_status"] == (
        "completed"
    )
    assert llm_debug.get("critic_verdict") is None
    assert llm_debug.get("completion_gate_verdict") is None
    assert llm_debug["turn_execution_diagnostics"].get("completion_gate") is None

    turn_record = llm_debug["turn_execution_record"]
    assert turn_record["schema_version"] == "turn_execution_record.observational.v1"
    assert turn_record["record_kind"] == "observational"
    assert "required_tool_obligations" not in turn_record
    assert "required_tool_obligation_ledger" not in turn_record
    assert "workflow_instance_id" not in payload


def test_ordinary_generate_does_not_run_disabled_buttonify_model(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.server.routes import von_routes

    def _unexpected_buttonify(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError(
            "disabled buttonify must not make a post-answer model call"
        )

    monkeypatch.setattr(von_routes, "_llm_generate_buttonify", _unexpected_buttonify)

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Answer without a compulsory second model call."},
    )

    assert response.status_code == 200
    llm_debug = response.get_json()["llm_debug"]
    assert llm_debug["buttonify"] is None
    buttonify_event = next(
        event
        for event in llm_debug["response_transformations"]["transformations"]
        if event.get("transform_name") == "buttonify"
    )
    assert buttonify_event["status"] == "skipped"
    assert buttonify_event["suppression_reason"] == "buttonify_disabled"
    assert buttonify_event["model_id"] is None


def test_ordinary_generate_requires_per_turn_opt_in_for_buttonify(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.server.routes import von_routes

    monkeypatch.setattr(von_routes, "get_buttonify_model_enabled", lambda: True)

    def _unexpected_buttonify(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError(
            "default generation must deliver before optional decoration"
        )

    monkeypatch.setattr(von_routes, "_llm_generate_buttonify", _unexpected_buttonify)

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Return the completed answer without post-answer work."},
    )

    assert response.status_code == 200
    llm_debug = response.get_json()["llm_debug"]
    assert llm_debug["buttonify"] is None
    buttonify_event = next(
        event
        for event in llm_debug["response_transformations"]["transformations"]
        if event.get("transform_name") == "buttonify"
    )
    assert buttonify_event["status"] == "skipped"
    assert buttonify_event["suppression_reason"] == "foreground_delivery_priority"
    assert buttonify_event["model_id"] is None


def test_generate_canonicalises_actor_scope_for_the_adaptive_turn(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda *_args, **_kwargs: {
            "organisation_id": "sail_lab",
            "chat_session_id": "session-org-normalisation-test",
            "role": "member",
        },
    )

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Use my represented research context."},
    )

    assert response.status_code == 200
    adaptive_call = app.config["_ADAPTIVE_TURN_CALLS"][-1]
    assert adaptive_call["user_concept_id"] == "#V#michael_witbrock"
    assert adaptive_call["org_concept_id"] == "#V#sail_lab"
    assert adaptive_call["user_namespace"] == "#V#michael_witbrock@sail_lab"


def test_generate_passes_authorised_workflow_inputs_to_adaptive_capabilities(
    app: Flask,
) -> None:
    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "Use the represented capability with these inputs.",
            "workflow_inputs": {
                "record_id": "#V#record",
                "maximum_results": 3,
            },
        },
    )

    assert response.status_code == 200
    adaptive_call = app.config["_ADAPTIVE_TURN_CALLS"][-1]
    assert adaptive_call["workflow_launch_inputs"] == {
        "record_id": "#V#record",
        "maximum_results": 3,
    }


def test_body_identity_cannot_override_actor_scope_passed_to_adaptive_turn(
    app: Flask,
) -> None:
    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "Use my represented research context.",
            "user_id": "#V#spoofed_user",
            "org_id": "#V#spoofed_org",
        },
    )

    assert response.status_code == 200
    adaptive_call = app.config["_ADAPTIVE_TURN_CALLS"][-1]
    assert adaptive_call["user_concept_id"] == "#V#michael_witbrock"
    assert adaptive_call["org_concept_id"] == "#V#sail_lab"
    assert adaptive_call["user_namespace"] == "#V#michael_witbrock@sail_lab"
    assert "#V#spoofed_user" not in str(adaptive_call["context"])
    assert "#V#spoofed_org" not in str(adaptive_call["context"])


def test_generate_threads_actor_namespace_through_history_reads_and_persistence(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.server.routes import von_routes

    history_reads: list[dict[str, Any]] = []
    history_writes: list[dict[str, Any]] = []

    def _get_chat_history_session_state(**kwargs: Any) -> dict[str, Any]:
        history_reads.append(dict(kwargs))
        return {
            "session_id": kwargs["session_id"],
            "history": [],
        }

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_state",
        _get_chat_history_session_state,
    )
    monkeypatch.setattr(
        von_routes,
        "_add_chat_history_message",
        lambda **kwargs: history_writes.append(dict(kwargs)),
    )

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Use my represented research context."},
        headers={"X-Von-Window-Session": "trusted-window-session"},
    )

    assert response.status_code == 200
    expected_namespace = "#V#michael_witbrock@sail_lab"
    assert history_reads
    assert all(read.get("namespace") == expected_namespace for read in history_reads)
    assert {write["message"]["role"] for write in history_writes} == {
        "user",
        "assistant",
    }
    assert all(write.get("namespace") == expected_namespace for write in history_writes)
    assert all(write.get("skip_rag_indexing") is True for write in history_writes)


def test_represented_user_prompt_reaches_adaptive_context_without_controller(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _user_id, **_kwargs: [
            {
                "concept_id": "#V#test_user_prompt",
                "content": "Please be terse.",
            }
        ],
    )

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Give me a concise answer."},
    )

    assert response.status_code == 200
    adaptive_context = app.config["_ADAPTIVE_TURN_CALLS"][-1]["context"]
    assert any(
        item.get("role") == "system"
        and "USER-SPECIFIC SYSTEM PROMPT" in str(item.get("content"))
        and "Please be terse." in str(item.get("content"))
        for item in adaptive_context
    )


def test_presenter_mode_projects_tagged_adaptive_answer(app: Flask) -> None:
    app.config["_ADAPTIVE_TURN_STATE"]["response_text"] = (
        "<spoken>A short talk track.</spoken>\n"
        "<screen>A grounded on-screen answer.</screen>"
    )

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Present the result.", "presenter_mode": True},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["response"] == "A grounded on-screen answer."
    assert payload["response_channels"] == {
        "spoken": "A short talk track.",
        "screen": "A grounded on-screen answer.",
        "format": "tagged_blocks_v1",
    }


def test_effect_finality_fallback_is_presented_literally_without_model_backfill(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.server.routes import von_routes

    fallback = (
        "That turn did not finish cleanly. An effect remains indeterminate; "
        "inspect represented state before retrying it."
    )
    adaptive_state = app.config["_ADAPTIVE_TURN_STATE"]
    adaptive_state["response_text"] = fallback
    adaptive_state["terminal_status"] = "model_error"
    adaptive_state["effect_finality_fallback"] = True

    def _unexpected_backfill(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("effect-finality fallback must remain literal")

    monkeypatch.setattr(
        von_routes,
        "_invoke_presenter_screen_backfill_prompt",
        _unexpected_backfill,
    )
    monkeypatch.setattr(
        von_routes,
        "_llm_generate_spoken_backfill",
        _unexpected_backfill,
    )

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Present the result.", "presenter_mode": True},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["response"] == fallback
    assert payload["response_channels"] == {
        "spoken": fallback,
        "screen": fallback,
        "format": "effect_finality_fallback_v1",
    }
    assert (
        payload["llm_debug"]["screen_backfill_second_pass_attempted"] is False
    )
    assert (
        payload["llm_debug"]["spoken_backfill_second_pass_attempted"] is False
    )


def test_pending_effect_answer_reaches_presenter_channels(app: Flask) -> None:
    instance_id = "workflow-instance-pending-route-1"
    adaptive_state = app.config["_ADAPTIVE_TURN_STATE"]
    adaptive_state["response_text"] = (
        "<spoken>The work is still running.</spoken>\n"
        "<screen>The work is still running. Exact workflow instance: "
        f"{instance_id}.</screen>"
    )
    adaptive_state["terminal_status"] = "effect_partially_completed"
    adaptive_state["effect_finality_fallback"] = False

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Present the result.", "presenter_mode": True},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["terminal_status"] == "effect_partially_completed"
    assert payload["response"] == (
        f"The work is still running. Exact workflow instance: {instance_id}."
    )
    assert payload["response_channels"] == {
        "spoken": "The work is still running.",
        "screen": (
            f"The work is still running. Exact workflow instance: {instance_id}."
        ),
        "format": "tagged_blocks_v1",
    }


def test_presenter_channel_parser_ignores_tags_inside_fenced_blocks() -> None:
    from src.backend.server.routes.von_routes import _extract_presenter_channels

    response_text = (
        "```html\n"
        "<screen>Ignore this example block.</screen>\n"
        "```\n"
        "<spoken>Real spoken text.</spoken>\n"
        "<screen>Real screen text.</screen>"
    )

    assert _extract_presenter_channels(response_text) == {
        "format": "tagged_blocks_v1",
        "extracted": True,
        "spoken": "Real spoken text.",
        "screen": "Real screen text.",
    }


def test_presenter_mode_generates_spoken_backfill_for_screen_only_answer(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.server.routes import von_routes

    app.config["_ADAPTIVE_TURN_STATE"]["response_text"] = (
        "<screen>A grounded visual summary.</screen>"
    )
    narration_calls: list[dict[str, Any]] = []

    def _spoken_backfill(
        _llm_client: Any,
        system: str,
        user: str,
        model: str,
        model_parameters: dict[str, Any] | None = None,
    ) -> str:
        narration_calls.append(
            {
                "system": system,
                "user": user,
                "model": model,
                "model_parameters": model_parameters,
            }
        )
        return "<spoken>A short grounded talk track.</spoken>"

    monkeypatch.setattr(von_routes, "_llm_generate_spoken_backfill", _spoken_backfill)

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Present the result.", "presenter_mode": True},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["response"] == "A grounded visual summary."
    assert payload["response_channels"] == {
        "spoken": "A short grounded talk track.",
        "screen": "A grounded visual summary.",
        "format": "narration_fallback_v1",
    }
    assert len(narration_calls) == 1
    assert narration_calls[0]["model"] == "test-model"
    assert "A grounded visual summary." in narration_calls[0]["user"]
    assert (
        payload["llm_debug"]["spoken_backfill_second_pass_attempted"] is True
    )
    assert payload["llm_debug"]["spoken_backfill_second_pass_reason"] == (
        "missing_spoken"
    )
    narration_call = next(
        call
        for call in payload["llm_debug"]["llm_interaction"]["calls"]
        if call.get("stage") == "narration"
    )
    assert narration_call["call_id"].endswith(":support-llm:1")
    assert narration_call["selected_model"] == "test-model"
    assert narration_call["effective_model"] is None
    assert narration_call["model_identity_source"] is None
    assert narration_call["provider_request_sent"] is True
    assert narration_call["status"] == "completed"
    assert payload["llm_debug"]["llm_usage_cost_summary"]["call_count"] == 2


def test_narration_eligibility_denial_is_recorded_without_failing_turn(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.languagemodels.llm_interface import (
        ModelExecutionEligibilityError,
    )
    from src.backend.server.routes import von_routes

    app.config["_ADAPTIVE_TURN_STATE"]["response_text"] = (
        "<screen>A grounded visual summary.</screen>"
    )

    def _denied_spoken_backfill(*_args: Any, **_kwargs: Any) -> str:
        raise ModelExecutionEligibilityError(
            "This model is not enabled for the active scope.",
            provider="openai",
            model="test-model",
        )

    monkeypatch.setattr(
        von_routes,
        "_llm_generate_spoken_backfill",
        _denied_spoken_backfill,
    )

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Present the result.", "presenter_mode": True},
    )

    assert response.status_code == 200
    payload = response.get_json()
    narration_call = next(
        call
        for call in payload["llm_debug"]["llm_interaction"]["calls"]
        if call.get("stage") == "narration"
    )
    assert narration_call["provider"] == "openai"
    assert narration_call["provider_request_sent"] is False
    assert narration_call["status"] == "failed"
    assert narration_call["failure_kind"] == "model_not_enabled"
    assert narration_call["error_class"] == "ModelExecutionEligibilityError"


def test_presenter_mode_uses_represented_prompt_to_backfill_missing_screen(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.server.routes import von_routes

    app.config["_ADAPTIVE_TURN_STATE"]["response_text"] = (
        "<spoken>A short grounded talk track.</spoken>"
    )

    def _prompt_fragments(_user_id: str, *, prompt_types: tuple[str, ...]):
        if prompt_types == ("#V#von_chat_screen_content_prompt",):
            return [
                {
                    "concept_id": "#V#screen_prompt_test",
                    "content": "Represent the grounded evidence for the screen.",
                }
            ]
        return []

    invocation_calls: list[dict[str, Any]] = []

    def _invoke_screen_prompt(**kwargs: Any) -> tuple[str, str]:
        invocation_calls.append(dict(kwargs))
        return (
            "<screen>A detailed grounded on-screen explanation.</screen>",
            kwargs["model_name"],
        )

    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        _prompt_fragments,
    )
    monkeypatch.setattr(
        von_routes,
        "_invoke_presenter_screen_backfill_prompt",
        _invoke_screen_prompt,
    )

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Present the result.", "presenter_mode": True},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["response_channels"] == {
        "spoken": "A short grounded talk track.",
        "screen": "A detailed grounded on-screen explanation.",
        "format": "screen_backfill_from_represented_prompt_v1",
    }
    assert len(invocation_calls) == 1
    invocation = invocation_calls[0]
    assert invocation["represented_screen_prompt"] == (
        "Represent the grounded evidence for the screen."
    )
    assert invocation["context_telemetry"]["prompt_concept_ids"] == [
        "#V#screen_prompt_test"
    ]
    assert payload["llm_debug"]["screen_backfill_second_pass_attempted"] is True
    assert payload["llm_debug"]["screen_backfill_second_pass_reason"] == (
        "missing_screen"
    )


def test_presenter_backfill_context_has_observations_without_semantic_gate() -> None:
    from src.backend.server.routes.generate_route_support import (
        _build_presenter_screen_backfill_context,
    )

    messages, telemetry = _build_presenter_screen_backfill_context(
        prompt_concept_ids=("#V#von_screen_content_prompt_for_witbrock",),
        backfill_reason="missing_screen",
        user_request="Present the explicit workflow result.",
        model_response="A plain grounded response.",
        tool_evidence_summary="Evidence handle: ev_example",
        existing_spoken=None,
        required_screen_json_fence=None,
        response_candidate_internal_status=False,
        response_candidate_tool_dump=False,
        response_candidate_duplicates_spoken=False,
        workflow_execution_summary={
            "workflow_id": "#V#example_workflow",
            "completed": True,
            "terminal_status": "completed",
        },
    )

    context_payload = json.loads(messages[0]["content"])
    assert context_payload["workflow_execution"]["workflow_id"] == (
        "#V#example_workflow"
    )
    assert "completion_gate" not in context_payload
    assert "response_candidate_blocked_by_completion_gate" not in (
        context_payload["rejected_candidate_reasons"]
    )
    assert telemetry["workflow_execution_present"] is True
    assert "completion_gate_present" not in telemetry


def test_adaptive_render_plan_remains_observational_route_output(app: Flask) -> None:
    render_plan = {
        "enabled": True,
        "render_mode": "screen_only",
        "reason": "model_selected_representation",
    }
    app.config["_ADAPTIVE_TURN_STATE"]["render_plan"] = render_plan

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Show the result clearly."},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["llm_debug"]["render_plan"] == render_plan


def test_adaptive_render_plan_record_set_becomes_valid_display_table(
    app: Flask,
) -> None:
    app.config["_ADAPTIVE_TURN_STATE"]["render_plan"] = {
        "enabled": True,
        "reason": "model_selected_representation",
        "render_mode": "screen_only",
        "screen_table_record_sets": [
            {
                "element_id": "screen_project_table",
                "intent": "structured_tabular_view",
                "records": [
                    {
                        "project_id": "#V#project_alpha",
                        "project_name": "Project Alpha",
                        "role": "principal investigator",
                        "source": {"source_concept_id": "#V#project_alpha"},
                    }
                ],
                "columns": [
                    {
                        "column_id": "project_name",
                        "label": "Project",
                        "source_key": "project_name",
                        "data_type": "text",
                    },
                    {
                        "column_id": "role",
                        "label": "Role",
                        "source_key": "role",
                        "data_type": "text",
                    },
                ],
                "row_id_field": "project_id",
                "row_provenance_field": "source",
            }
        ],
    }

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Show my represented research projects."},
    )

    assert response.status_code == 200
    payload = response.get_json()
    display_elements = payload["display_elements"]
    assert display_elements["validation"]["valid"] is True
    assert "screen_structured_tables_supplied" in display_elements["reason_codes"]
    table = next(
        element
        for element in display_elements["elements"]
        if element.get("element_id") == "screen_project_table"
    )
    assert table["element_type"] == "table"
    assert table["payload"]["rows"][0]["row_id"] == "#V#project_alpha"
    assert table["payload"]["rows"][0]["provenance"] == {
        "source_concept_id": "#V#project_alpha"
    }


def test_background_adaptive_result_serialisation_preserves_render_plan() -> None:
    from src.backend.server.routes.von_routes import (
        _serialise_background_turn_result,
    )

    render_plan = {
        "enabled": True,
        "reason": "model_selected_representation",
        "render_mode": "screen_only",
    }
    result = AdaptiveTurnResult(
        response_text="A represented project summary.",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
        render_plan=render_plan,
    )

    serialised = _serialise_background_turn_result(result)

    assert serialised["render_plan"] == render_plan
    assert serialised["llm_debug"]["render_plan"] == render_plan


def test_adaptive_read_summary_log_omits_arguments_and_evidence(
    app: Flask,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_marker = "PRIVATE-MAIL-CONTENT-DO-NOT-LOG"
    private_context_marker = "PRIVATE-CONTEXT-CONTENT-DO-NOT-LOG"
    app.config["CONTEXT"] = [
        {
            "role": "system",
            "content": private_context_marker,
        }
    ]
    app.config["_ADAPTIVE_TURN_STATE"]["tool_invocations"] = (
        {
            "tool": "gmail_get_message",
            "arguments": {
                "message_id": "private-message-id",
                "query": private_marker,
            },
            "effective_arguments": {
                "profile": "represented-private-profile",
            },
            "result": {
                "subject": private_marker,
                "body": private_marker,
            },
            "result_summary": private_marker,
        },
    )

    with caplog.at_level(logging.INFO):
        response = app.test_client().post(
            "/von/generate",
            json={"prompt": "Summarise the relevant message."},
        )

    assert response.status_code == 200
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "Read capability summary: count=1" in log_text
    assert "gmail_get_message" in log_text
    assert private_marker not in log_text
    assert private_context_marker not in log_text
    assert "private-message-id" not in log_text
    assert "represented-private-profile" not in log_text
