from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from src.backend.services.selected_workflow_handoff_service import (
    evaluate_selected_workflow_handoff,
    workflow_result_tool_invocations,
)
from src.backend.workflows.execution_contracts import (
    WORKFLOW_STEP_RESULT_ENVELOPES_KEY,
)


def _derive_missing_prompt_requirements(
    *,
    required_tools: Sequence[str],
    required_fetch_concept_ids: Sequence[str],
    required_read_file_copy_ids: Sequence[str] = (),
    required_scholarly_representation_for_file_copy_ids: Sequence[str] = (),
    tool_invocations: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[str], list[str], list[str]]:
    del required_fetch_concept_ids
    del required_read_file_copy_ids
    del required_scholarly_representation_for_file_copy_ids

    invoked_tools = {
        str(invocation.get("tool")).strip().lower()
        for invocation in tool_invocations
        if isinstance(invocation.get("tool"), str)
        and str(invocation.get("tool")).strip()
    }
    missing_tools = [
        tool
        for tool in required_tools
        if isinstance(tool, str) and tool.strip().lower() not in invoked_tools
    ]
    return missing_tools, [], [], []


def _workflow_definition(
    *,
    contract: Mapping[str, Any] | None = None,
    actions: Sequence[Any] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata={"required_effects_contract": contract} if contract else {},
        states={"start": SimpleNamespace(actions=list(actions))},
    )


def _contract(required_tools: Sequence[str]) -> dict[str, Any]:
    return {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "selected_workflow_required_effects",
        "required_effects": [
            {
                "effect_id": "selected_workflow_evidence",
                "effect_type": "grounded_evidence",
                "required_tools": list(required_tools),
                "required_tools_match": "all",
            }
        ],
    }


def test_failed_selected_workflow_before_tool_progress_hands_off() -> None:
    workflow_result = SimpleNamespace(
        completed=False,
        final_state="launch_inputs",
        error="workflow_launch_input_resolution_failed:paper_url",
        data={"summary": "The selected workflow could not resolve paper_url."},
    )

    decision = evaluate_selected_workflow_handoff(
        selected_workflow_id="#V#selected_workflow",
        workflow_result=workflow_result,
        selected_workflow_definition=None,
        routing_required_tools=(),
        routing_required_fetch_concept_ids=(),
        routing_required_read_file_copy_ids=(),
        routing_required_scholarly_representation_file_copy_ids=(),
        fallback_tool_workflow_id="#V#tool_calling_workflow",
        derive_missing_prompt_requirements=_derive_missing_prompt_requirements,
    )

    assert decision.continue_to_tool_pipeline is True
    assert decision.reason == "failed_custom_workflow_before_tool_progress"
    assert decision.required_tools_for_tool_pipeline == ()
    assert decision.selected_workflow_snapshot == {
        "workflow_id": "#V#selected_workflow",
        "completed": False,
        "tool_progress_detected": False,
        "final_state": "launch_inputs",
        "operational_error": "workflow_launch_input_resolution_failed:paper_url",
        "user_visible_failure_text": (
            "The selected workflow could not resolve paper_url."
        ),
    }
    assert decision.recovery_handoff_payload is not None
    assert decision.recovery_handoff_payload["failed_workflow_snapshot"] == (
        decision.selected_workflow_snapshot
    )


def test_completed_selected_workflow_missing_external_evidence_hands_off() -> None:
    contract = _contract(["fetch_concept", "get_text_relations_summary"])
    workflow_result = SimpleNamespace(
        completed=True,
        final_state="complete",
        error=None,
        data={
            "workflow_required_effects_contract": contract,
            "invocations": [{"tool": "fetch_concept", "status": "ok"}],
        },
    )
    workflow_definition = _workflow_definition(
        contract=contract,
        actions=(
            SimpleNamespace(
                action_id="workflow.plan",
                is_llm_step=True,
                llm_policy={
                    "allowed_tools": [
                        "fetch_concept",
                        "get_text_relations_summary",
                    ]
                },
            ),
        ),
    )

    decision = evaluate_selected_workflow_handoff(
        selected_workflow_id="#V#selected_workflow",
        workflow_result=workflow_result,
        selected_workflow_definition=workflow_definition,
        routing_required_tools=(),
        routing_required_fetch_concept_ids=(),
        routing_required_read_file_copy_ids=(),
        routing_required_scholarly_representation_file_copy_ids=(),
        fallback_tool_workflow_id="#V#tool_calling_workflow",
        derive_missing_prompt_requirements=_derive_missing_prompt_requirements,
    )

    assert decision.continue_to_tool_pipeline is True
    assert decision.reason == "custom_workflow_missing_required_prompt_tools"
    assert decision.missing_required_tools == ("get_text_relations_summary",)
    assert decision.required_tools_for_tool_pipeline == ("get_text_relations_summary",)
    assert decision.selected_workflow_snapshot == {
        "workflow_id": "#V#selected_workflow",
        "completed": True,
        "tool_progress_detected": True,
        "missing_required_tools": ["get_text_relations_summary"],
        "recovery_reason": "custom_workflow_missing_required_prompt_tools",
        "final_state": "complete",
    }
    assert decision.evidence_classification["required_tools_source"] == (
        "workflow_required_effects_contract"
    )
    assert decision.evidence_classification["missing_required_tools"] == [
        "get_text_relations_summary"
    ]


def test_workflow_step_action_evidence_satisfies_routing_required_tools() -> None:
    workflow_result = SimpleNamespace(
        completed=True,
        final_state="complete",
        error=None,
        data={
            WORKFLOW_STEP_RESULT_ENVELOPES_KEY: [
                {
                    "action_id": "paper_representation.finalise",
                    "action_status": "success",
                    "output_payload": {"success": True},
                }
            ]
        },
    )

    decision = evaluate_selected_workflow_handoff(
        selected_workflow_id="#V#paper_representation_workflow",
        workflow_result=workflow_result,
        selected_workflow_definition=None,
        routing_required_tools=("paper_representation.finalise",),
        routing_required_fetch_concept_ids=(),
        routing_required_read_file_copy_ids=(),
        routing_required_scholarly_representation_file_copy_ids=(),
        fallback_tool_workflow_id="#V#tool_calling_workflow",
        derive_missing_prompt_requirements=_derive_missing_prompt_requirements,
    )

    assert decision.continue_to_tool_pipeline is False
    assert decision.reason is None
    assert decision.missing_required_tools == ()
    assert decision.evidence_classification["prompt_required_tools"] == [
        "paper_representation.finalise"
    ]


def test_workflow_result_tool_invocations_omits_internal_steps_without_required_tool() -> None:
    workflow_result = SimpleNamespace(
        completed=True,
        final_state="complete",
        error=None,
        data={
            WORKFLOW_STEP_RESULT_ENVELOPES_KEY: [
                {
                    "action_id": "turn_execution.critic",
                    "action_status": "success",
                    "output_payload": {"success": True},
                },
                {
                    "action_id": "workflow_mcp.invoke_tool",
                    "action_status": "success",
                    "output_payload": {
                        "mcp_requested_tool": "gmail_get_message",
                        "mcp_resolved_tool": "gmail_get_message",
                        "success": True,
                    },
                },
            ]
        },
    )

    invocations = workflow_result_tool_invocations(workflow_result)

    assert [invocation.get("tool") for invocation in invocations] == [
        "gmail_get_message"
    ]


def test_direct_action_required_effects_do_not_trigger_tool_pipeline() -> None:
    contract = _contract(["paper_representation.finalise"])
    workflow_result = SimpleNamespace(
        completed=True,
        final_state="complete",
        error=None,
        data={"workflow_required_effects_contract": contract},
    )
    workflow_definition = _workflow_definition(
        contract=contract,
        actions=(
            SimpleNamespace(
                action_id="paper_representation.finalise",
                is_llm_step=False,
                llm_policy={},
            ),
        ),
    )

    decision = evaluate_selected_workflow_handoff(
        selected_workflow_id="#V#paper_representation_workflow",
        workflow_result=workflow_result,
        selected_workflow_definition=workflow_definition,
        routing_required_tools=(),
        routing_required_fetch_concept_ids=(),
        routing_required_read_file_copy_ids=(),
        routing_required_scholarly_representation_file_copy_ids=(),
        fallback_tool_workflow_id="#V#tool_calling_workflow",
        derive_missing_prompt_requirements=_derive_missing_prompt_requirements,
    )

    assert decision.continue_to_tool_pipeline is False
    assert decision.reason is None
    assert decision.missing_required_tools == ()
    assert decision.evidence_classification[
        "filtered_workflow_internal_action_tools"
    ] == ["paper_representation.finalise"]
