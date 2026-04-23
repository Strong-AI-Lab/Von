from __future__ import annotations

from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    OrchestratorResult,
    _PromptRequirementEvaluation,
)
from src.backend.workflows.durable.turn_execution_runtime_support import (
    build_turn_execution_selected_workflow_outputs,
)


class _Gateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}


def test_materialise_tool_calling_orchestrator_result_preserves_aux_and_llm_calls() -> (
    None
):
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _Gateway()))
    data: dict[str, Any] = {
        "invocations": [],
        "tool_messages": [],
        "aux_llm_calls": [],
        "llm_calls": [],
    }

    orchestrator._materialise_tool_calling_orchestrator_result(
        data,
        OrchestratorResult(
            response_text="Grounded answer.",
            extra_messages=({"role": "tool", "content": "tool output"},),
            tool_invocations=(
                {"tool": "get_predicate_incidence", "payload": {"concept_id": "#V#u"}},
            ),
            aux_llm_calls=(
                {
                    "type": "workflow_dispatch_boundary",
                    "boundary": "workflow_handoff",
                    "status": "started",
                },
            ),
            llm_calls=(
                {
                    "stage": "summariser",
                    "prompt": "Provide a final answer",
                    "response": "Grounded answer.",
                },
            ),
        ),
    )

    assert data["final_response"] == "Grounded answer."
    assert data["current_response"] == "Grounded answer."
    assert data["invocations"] == [
        {"tool": "get_predicate_incidence", "payload": {"concept_id": "#V#u"}}
    ]
    assert data["tool_messages"] == [{"role": "tool", "content": "tool output"}]
    assert data["aux_llm_calls"] == [
        {
            "type": "workflow_dispatch_boundary",
            "boundary": "workflow_handoff",
            "status": "started",
        }
    ]
    assert data["llm_calls"] == [
        {
            "stage": "summariser",
            "prompt": "Provide a final answer",
            "response": "Grounded answer.",
        }
    ]


def test_build_tool_calling_state_outputs_preserves_prompt_requirement_state() -> None:
    outputs = InternalMCPChatOrchestrator._build_tool_calling_state_outputs(
        {
            "final_response": "Grounded answer.",
            "current_response": "Grounded answer.",
            "invocations": [{"tool": "get_predicate_incidence"}],
            "tool_messages": [{"role": "tool", "content": "predicate summary"}],
            "aux_llm_calls": [{"type": "prompt_tool_requirements"}],
            "llm_calls": [{"stage": "tool_execute"}],
            "required_prompt_tools": [
                "get_predicate_incidence",
                "find_relations_with_argument",
            ],
            "missing_prompt_tools": [],
            "prompt_requirements_preflight_completed": True,
            "tool_follow_up_context_lineage": {"stage": "summariser"},
        }
    )

    assert outputs["required_prompt_tools"] == [
        "get_predicate_incidence",
        "find_relations_with_argument",
    ]
    assert outputs["prompt_requirements_preflight_completed"] is True
    assert outputs["aux_llm_calls"] == [{"type": "prompt_tool_requirements"}]
    assert outputs["llm_calls"] == [{"stage": "tool_execute"}]
    assert outputs["tool_follow_up_context_lineage"] == {"stage": "summariser"}


def test_store_prompt_requirement_evaluation_sets_effective_allowed_tools() -> None:
    data: dict[str, Any] = {}

    InternalMCPChatOrchestrator._store_prompt_requirement_evaluation(
        data,
        _PromptRequirementEvaluation(
            required_tools=(
                "get_predicate_incidence",
                "find_relations_with_argument",
            )
        ),
    )

    assert data["required_prompt_tools"] == [
        "get_predicate_incidence",
        "find_relations_with_argument",
    ]
    assert data["llm_allowed_tools"] == [
        "get_predicate_incidence",
        "find_relations_with_argument",
    ]


def test_selected_workflow_outputs_preserve_child_telemetry_and_required_tools() -> (
    None
):
    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#tool_calling_workflow",
        child_completed=True,
        final_state="completed",
        failure_detail=None,
        child_outputs={
            "response_text": "Grounded answer.",
            "final_response": "Grounded answer.",
            "invocations": [{"tool": "get_predicate_incidence"}],
            "tool_messages": [{"role": "tool", "content": "predicate summary"}],
            "aux_llm_calls": [
                {
                    "type": "workflow_dispatch_boundary",
                    "boundary": "workflow_terminal",
                    "status": "completed",
                }
            ],
            "llm_calls": [{"stage": "summariser"}],
            "required_prompt_tools": [
                "get_predicate_incidence",
                "find_relations_with_argument",
            ],
            "prompt_requirements_preflight_completed": True,
            "tool_follow_up_context_lineage": {"stage": "summariser"},
        },
        rendered_child_response_text="Grounded answer.",
        child_result_snapshot={"response_text": "Grounded answer."},
    )

    assert outputs["response_text"] == "Grounded answer."
    assert outputs["invocations"] == [{"tool": "get_predicate_incidence"}]
    assert outputs["aux_llm_calls"] == [
        {
            "type": "workflow_dispatch_boundary",
            "boundary": "workflow_terminal",
            "status": "completed",
        }
    ]
    assert outputs["llm_calls"] == [{"stage": "summariser"}]
    assert outputs["required_prompt_tools"] == [
        "get_predicate_incidence",
        "find_relations_with_argument",
    ]
    assert outputs["prompt_requirements_preflight_completed"] is True
    assert outputs["tool_follow_up_context_lineage"] == {"stage": "summariser"}
