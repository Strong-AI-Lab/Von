from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from src.backend.workflows.action_registry import (
    ActionRegistry,
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.workflows.definitions import (
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
)
from src.backend.workflows.durable.subworkflow_actions import register_subworkflow_actions
from src.backend.workflows.durable.turn_execution_runtime_support import (
    run_turn_execution_completion_gate,
)
from src.backend.workflows.llm_step_executor import execute_llm_step
from src.backend.workflows.subworkflow_contracts import WORKFLOW_SUBWORKFLOW_ACTION_ID


class _ExplodingLLM:
    def generate(self, *_args: Any, **_kwargs: Any) -> str:  # pragma: no cover
        raise AssertionError("AgentTest fast path should not call the LLM")


class _DummyGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}


def _expected_outcome_validation_policy() -> dict[str, Any]:
    defaults = {
        "answering_guidance": "default answering guidance",
        "expected_outcome_summary": "default summary",
        "grounding_requirement": "default grounding",
        "precision_policy": "default precision",
        "required_tools": [],
        "reasoning": "default reasoning",
        "selector_guidance": "default selector guidance",
    }
    return {
        "json_field_defaults": defaults,
        "output_format": "json_value",
        "required_json_fields": list(defaults.keys()),
    }


def test_agent_test_workflow_experience_prelude_returns_empty_guidance(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    registry = ActionRegistry()

    def _unexpected_definition_loader(_workflow_id: str) -> None:
        raise AssertionError("AgentTest prelude bypass should not load definitions")

    register_subworkflow_actions(
        registry,
        definition_loader=_unexpected_definition_loader,
    )
    context: dict[str, Any] = {
        "requested_model": "gemma4:e4b",
        "selected_workflow_id": CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    }

    result = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
            "workflow_experience_target_workflow_id": (
                CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
            ),
            "failure_mode": "capture",
        },
        context=context,
        env=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
    )

    assert result.status == "success"
    payload = result.outputs["result"]
    assert payload["workflow_success_guidance_history"] == []
    assert payload["workflow_failure_avoidance_history"] == []
    assert payload["workflow_low_imposition_exploration_history"] == []
    assert payload["workflow_experience_profile_concept_id"].startswith("#V#")
    assert result.outputs["subworkflow_invocation"]["child_final_state"] == (
        "agent_test_skipped"
    )


def test_agent_test_postcondition_critic_subworkflow_uses_deterministic_record(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    registry = ActionRegistry()

    def _unexpected_definition_loader(_workflow_id: str) -> None:
        raise AssertionError("AgentTest critic bypass should not load definitions")

    register_subworkflow_actions(
        registry,
        definition_loader=_unexpected_definition_loader,
    )
    context: dict[str, Any] = {
        "prompt": "What text relations are used with the concept for Michael Witbrock?",
        "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
        "final_response": "For #V#michael_witbrock, the text-relation summary found hasName.",
        "requested_model": "gemma4:e4b",
        "required_prompt_tools": ["get_text_relations_summary"],
        "invocations": [
            {
                "tool": "get_text_relations_summary",
                "status": "ok",
                "payload": {
                    "concept_id": "#V#michael_witbrock",
                    "groups_found": 1,
                    "total_relations_scanned": 1,
                    "groups": [{"predicate": "hasName", "count": 1}],
                },
            }
        ],
        "aux_llm_calls": [],
    }

    result = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
            "failure_mode": "capture",
        },
        context=context,
        env=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
    )

    assert result.status == "success"
    assert result.outputs["subworkflow_invocation"]["child_final_state"] == (
        "agent_test_deterministic_critic"
    )
    payload = result.outputs["result"]
    assert payload["agent_test_critic_skip_reason"] == "agent_test_instance"
    assert payload["completion_gate_requires_follow_up"] is False
    assert payload["turn_execution_record"]["execution"]["required_prompt_tools"] == [
        "get_text_relations_summary"
    ]


def test_agent_test_expected_outcome_fast_path_marks_relation_tools(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
            "requested_model": "gemma4:e4b",
            "selected_model_provider": "ollama",
        },
        llm_policy={"policy_stage": "planner"},
        validation_policy=_expected_outcome_validation_policy(),
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="expected_outcome_inference",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    payload = result.outputs["validated_json"]
    assert payload["required_tools"] == ["get_text_relations_summary"]
    assert "tool-calling workflow" in payload["selector_guidance"]


def test_agent_test_selector_fast_path_routes_relation_prompt_to_tools(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
            "requested_model": "gemma4:e4b",
            "selected_model_provider": "ollama",
            "turn_expected_required_tools": ["get_text_relations"],
        },
        llm_policy={"policy_stage": "classifier"},
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="selector_decision",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    selector_payload = json.loads(result.outputs["final_response"])
    assert selector_payload["workflow_id"] == TOOL_CALLING_WORKFLOW_ID


def test_agent_test_selector_preparation_uses_local_tool_candidate(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())

    def _unexpected_discovery(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("AgentTest selector prep should not call discovery")

    def _unexpected_prompt_render(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("AgentTest selector prep should not render prompts")

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
        _unexpected_discovery,
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_current_request_stage_message",
        _unexpected_prompt_render,
    )
    request = type(
        "Request",
        (),
        {
            "data": {
                "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
                "requested_model": "gemma4:e4b",
                "selected_model_provider": "ollama",
                "turn_expected_required_tools": ["get_text_relations"],
            },
            "environment": WorkflowEnvironment(
                llm_client=_ExplodingLLM(),
                model="gemma4:e4b",
                user_namespace="#V#michael_witbrock",
            ),
        },
    )()

    outputs = orchestrator._prepare_turn_selector_context_outputs(request)

    assert outputs["selector_prompt_available"] is True
    assert outputs["selector_candidate_ids"] == [TOOL_CALLING_WORKFLOW_ID]
    assert outputs["workflow_discovery_result"]["agent_test_local_replay"] is True


def test_agent_test_narration_fast_path_reuses_selected_workflow_response(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "selected_workflow_user_response": "Michael Witbrock has relation evidence.",
            "requested_model": "gemma4:e4b",
            "selected_model_provider": "ollama",
        },
        llm_policy={"policy_stage": "narration"},
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        workflow_state_id="narration",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert result.outputs["final_response"] == (
        "Michael Witbrock has relation evidence."
    )


def test_agent_test_completion_gate_skips_episode_autotrigger(monkeypatch) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")

    import src.backend.services.workflow_event_integration_service as event_integration

    monkeypatch.setattr(
        event_integration,
        "episode_evaluation_autotrigger_enabled",
        lambda: (_ for _ in ()).throw(
            AssertionError("AgentTest completion gate should not query autotrigger")
        ),
    )
    monkeypatch.setattr(
        event_integration,
        "maybe_launch_episode_evaluation_for_turn_completion_gate",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("AgentTest completion gate should not launch episodes")
        ),
    )
    request = WorkflowActionRequest(
        action_id="turn_execution.completion_gate",
        inputs={},
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "prompt": "What text relations are used with the concept for Michael Witbrock?",
            "final_response": "Michael Witbrock has text relation evidence.",
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "completed",
                    "safe_to_claim_completion": True,
                    "requires_follow_up": False,
                },
                "required_effects": [],
                "critic": {"summary": {}},
            },
            "aux_llm_calls": [],
        },
    )

    result = run_turn_execution_completion_gate(
        request,
        annotation_component="test",
        annotation_function="test_agent_test_completion_gate",
        introspection_auto_apply_env="VON_TEST_AUTO_APPLY",
    )

    assert result.status == "success"
    assert result.outputs["workflow_introspection_autotrigger"]["reason"] == (
        "agent_test_instance"
    )


def test_agent_test_tool_calling_plan_uses_relation_summary(monkeypatch) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    request = SimpleNamespace(
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        action_id="tool_calling.plan",
        data={
            "prompt": "What text relations are used with the concept for Michael Witbrock?",
            "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
            "user_concept_id": "#V#michael_witbrock",
            "augmented_context": [],
            "policy_state": SimpleNamespace(enabled=False, policy=None),
            "model_for_stage": lambda _stage: "gemma4:e4b",
            "record_llm_call": lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("AgentTest relation plan should not call the LLM")
            ),
            "aux_llm_calls": [],
            "llm_calls": [],
        },
    )

    result = orchestrator._action_tool_calling_plan(request)

    assert result.status == "success"
    assert result.outputs["tool_calls_present"] is True
    assert result.outputs["tool_calls"] == [
        {
            "action": "call_tool",
            "tool": "get_text_relations_summary",
            "payload": {"concept_id": "#V#michael_witbrock"},
        }
    ]
    assert result.outputs["required_prompt_tools"] == ["get_text_relations_summary"]


def test_agent_test_tool_calling_backfill_uses_relation_summary(monkeypatch) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())
    request = SimpleNamespace(
        environment=WorkflowEnvironment(llm_client=_ExplodingLLM(), model="gemma4:e4b"),
        data={
            "prompt": "What text relations are used with the concept for Michael Witbrock?",
            "user_prompt": "What text relations are used with the concept for Michael Witbrock?",
            "augmented_context": [],
            "policy_state": SimpleNamespace(enabled=False, policy=None),
            "model_for_stage": lambda _stage: (_ for _ in ()).throw(
                AssertionError("AgentTest relation backfill should not call the LLM")
            ),
            "record_llm_call": lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("AgentTest relation backfill should not call the LLM")
            ),
            "aux_llm_calls": [],
            "llm_calls": [],
            "invocations": [
                {
                    "tool": "get_text_relations_summary",
                    "status": "ok",
                    "payload": {
                        "concept_id": "#V#michael_witbrock",
                    },
                    "effective_payload": {
                        "concept_id": "#V#michael_witbrock",
                        "groups_found": 2,
                        "total_relations_scanned": 3,
                        "predicates": ["hasName", "#V#has_email"],
                        "groups": [
                            {"predicate": "hasName", "language": "en-NZ", "count": 2},
                            {"predicate": "#V#has_email", "language": "en", "count": 1},
                        ],
                    },
                }
            ],
        },
    )

    result = orchestrator._action_tool_calling_backfill(request)

    assert result.status == "success"
    assert result.outputs["more_tool_calls"] is False
    assert result.outputs["required_prompt_tools"] == ["get_text_relations_summary"]
    assert "hasName" in result.outputs["final_response"]
    assert "#V#has_email" in result.outputs["final_response"]
    assert "did not expose predicate names" not in result.outputs["final_response"]
