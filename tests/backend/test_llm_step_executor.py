from __future__ import annotations

from unittest.mock import MagicMock

from src.backend.workflows.action_registry import (
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.workflows.llm_step_executor import execute_llm_step


def _build_request(*, llm_response: str) -> WorkflowActionRequest:
    llm_client = MagicMock()
    llm_client.generate.return_value = llm_response
    return WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=llm_client),
        data={"invitation_text": "Please meet on Monday at 10am."},
        prompt_contract={
            "prompt_text": "Return JSON only for the meeting invitation.",
        },
        validation_policy={"output_format": "json_value"},
    )


def test_execute_llm_step_parses_json_value_output() -> None:
    request = _build_request(
        llm_response='{"meeting_type":"project_meeting","title":"Roadmap sync"}'
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert result.outputs["validated_json"] == {
        "meeting_type": "project_meeting",
        "title": "Roadmap sync",
    }
    assert result.outputs["validated_json_parse_mode"] in {
        "strict_json",
        "direct_json",
    }
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validation"]["status"] == "success"
    assert envelope["validation"]["output_format"] == "json_value"


def test_execute_llm_step_fails_closed_when_json_value_is_invalid() -> None:
    request = _build_request(llm_response="This is not valid JSON.")

    result = execute_llm_step(request)

    assert result.status == "failed"
    assert "json_parse_failed" in str(result.error or "")
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validation"]["status"] == "failed"
    assert "json_parse_failed" in str(envelope["validation"]["reason"] or "")


def test_execute_llm_step_passes_context_lineage_to_gateway_llm(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            captured["context"] = kwargs.get("context")
            captured["context_telemetry"] = kwargs.get("context_telemetry")
            captured["prefer_default_model"] = kwargs.get("prefer_default_model")
            return ('{"ok": true}', "test-model", None)

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), None, None, None),
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="test-model",
        ),
        data={
            "requested_model": "gemma4:26b",
            "selector_context_messages": [
                {"role": "system", "content": "Selector prompt"},
                {"role": "user", "content": "Who am I?"},
            ],
            "selector_context_lineage": {
                "stage": "selector_decision",
                "base_context_source": "augmented_context",
                "stage_added_message_count": 1,
            },
        },
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={
            "context_messages_context_key": "selector_context_messages",
            "context_lineage_context_key": "selector_context_lineage",
        },
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert captured["context"] == [
        {"role": "system", "content": "Selector prompt"},
        {"role": "user", "content": "Who am I?"},
    ]
    assert captured["context_telemetry"] == {
        "stage": "selector_decision",
        "base_context_source": "augmented_context",
        "stage_added_message_count": 1,
    }
    assert captured["prefer_default_model"] is True


def test_execute_llm_step_tool_mode_marks_user_model_preference(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _StubGateway:
        def describe_methods(self) -> dict[str, object]:
            return {}

    class _StubOrchestrator:
        def __init__(self, **_kwargs):
            pass

        def _load_workflow_model_policy(self, _preferred_language):
            return object(), {}

        def _select_model_for_stage(self, **kwargs):
            captured["prefer_default_model"] = kwargs.get("prefer_default_model")
            return kwargs.get("default_model")

        def _action_tool_calling_plan(self, request):
            captured["shared_prefer_default_model"] = request.data.get(
                "prefer_default_model"
            )
            request.data["model_for_stage"]("tool_call")
            return type(
                "_Result",
                (),
                {
                    "status": "success",
                    "outputs": {
                        "tool_calls_present": False,
                        "orchestrator_result": {"response_text": '{"ok": true}'},
                    },
                },
            )()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _StubOrchestrator,
    )
    monkeypatch.setattr(
        "src.backend.services.model_registry_service.get_model_registry_snapshot",
        lambda: {},
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=_StubGateway(),
            model="gemma4:26b",
        ),
        data={
            "requested_model": "gemma4:26b",
            "requested_client_type": "ollama",
        },
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={"tool_mode": "allowed"},
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert captured["shared_prefer_default_model"] is True
    assert captured["prefer_default_model"] is True


def test_execute_llm_step_emits_phase_transition_for_conversation_turn_stage(
    monkeypatch,
) -> None:
    transitions: list[dict[str, object]] = []

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            return ('{"ok": true}', "test-model", None)

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), None, None, None),
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="test-model",
        ),
        data={
            "emit_phase_transition": (
                lambda phase, *, extra=None: transitions.append(
                    {"phase": phase, "extra": extra}
                )
            )
        },
        workflow_state_id="selector_decision",
        prompt_contract={"prompt_text": "Return JSON only."},
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert transitions == [
        {
            "phase": "selector_decision",
            "extra": {
                "result_summary": (
                    "Running the authoritative LLM reasoning step for this turn stage"
                ),
                "workflow_state_id": "selector_decision",
            },
        }
    ]
