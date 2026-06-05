from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    OrchestratorResult,
    _PromptRequirementEvaluation,
)
from src.backend.services.turn_execution_record_service import (
    build_turn_execution_record,
)
from src.backend.workflows.durable.turn_execution_runtime_support import (
    build_turn_execution_selected_workflow_outputs,
    render_selected_workflow_user_response,
)
from src.backend.workflows.execution_contracts import WORKFLOW_STEP_RESULT_ENVELOPES_KEY


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


def test_prompt_requirement_merge_preserves_workflow_tool_policy() -> None:
    data: dict[str, Any] = {
        "required_prompt_tools": [
            "fetch_concept",
            "get_text_relations_summary",
            "find_relations_with_argument",
        ],
        "llm_allowed_tools": [
            "fetch_concept",
            "get_text_relations_summary",
            "find_relations_with_argument",
            "search_concepts",
        ],
    }
    evaluation = _PromptRequirementEvaluation(
        required_tools=("fetch_concept", "get_related_concepts"),
    )

    merged = InternalMCPChatOrchestrator._merge_prompt_requirements_with_existing_tool_policy(
        data=data,
        evaluation=evaluation,
        method_catalogue={
            "fetch_concept": {},
            "get_text_relations_summary": {},
            "find_relations_with_argument": {},
            "search_concepts": {},
        },
    )
    InternalMCPChatOrchestrator._store_prompt_requirement_evaluation(data, merged)

    assert list(merged.required_tools) == [
        "fetch_concept",
        "get_text_relations_summary",
        "find_relations_with_argument",
    ]
    assert "get_related_concepts" not in merged.required_tools
    assert data["llm_allowed_tools"] == [
        "fetch_concept",
        "get_text_relations_summary",
        "find_relations_with_argument",
        "search_concepts",
    ]


def test_selected_workflow_outputs_preserve_child_telemetry_and_required_tools() -> (
    None
):
    workflow_required_effects_contract = {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "grounded_entity_information_retrieval_evidence",
        "required_effects": [
            {
                "effect_id": "grounded_entity_information_evidence",
                "effect_type": "grounded_evidence",
                "required_tools": [
                    "get_predicate_incidence",
                    "find_relations_with_argument",
                ],
                "required_tools_match": "all",
            }
        ],
    }
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
            "workflow_required_effects_contract": workflow_required_effects_contract,
            "workflow_required_effects_contract_source": "definition_metadata",
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
    assert (
        outputs["completion_report"]["workflow_required_effects_contract"]
        == workflow_required_effects_contract
    )
    assert (
        outputs["selected_workflow_trace"]["workflow_required_effects_contract"]
        == workflow_required_effects_contract
    )
    assert (
        outputs["selected_workflow_trace"]["workflow_required_effects_contract_source"]
        == "definition_metadata"
    )


def test_selected_workflow_outputs_preserve_parent_dispatch_telemetry() -> None:
    parent_aux = [
        {
            "type": "workflow_dispatch_boundary",
            "boundary": "workflow_handoff",
            "status": "started",
            "selected_execution_mode": "tool_pipeline",
            "selected_workflow_id": "#V#tool_calling_workflow",
        },
        {
            "type": "workflow_dispatch_boundary",
            "boundary": "workflow_terminal",
            "status": "failed",
            "selected_execution_mode": "tool_pipeline",
            "selected_workflow_id": "#V#tool_calling_workflow",
            "error": (
                "workflow_required_effects_tools_unavailable:"
                "conversation_telemetry_get_locator"
            ),
        },
    ]
    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#tool_calling_workflow",
        child_completed=False,
        final_state="preflight_requirements",
        failure_detail=(
            "workflow_required_effects_tools_unavailable:"
            "conversation_telemetry_get_locator"
        ),
        child_outputs={
            "response_text": "Workflow could not start.",
            "workflow_required_effects_tool_policy": {
                "ok": False,
                "unavailable_required_tools": ["conversation_telemetry_get_locator"],
            },
        },
        rendered_child_response_text="Workflow could not start.",
        child_result_snapshot={"response_text": "Workflow could not start."},
        parent_aux_llm_calls=parent_aux,
    )

    assert outputs["aux_llm_calls"] == parent_aux


def test_selected_workflow_outputs_surface_nested_paper_concept_handles() -> None:
    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#represent_papers_from_arxiv_results_workflow",
        child_completed=True,
        final_state="completed",
        failure_detail=None,
        child_outputs={
            "response_text": "Represented the requested arXiv papers.",
            "workflow_execution_summary": {
                "result": {
                    "successful_results": [
                        {
                            "arxiv_id": "2402.18144",
                            "paper_concept_id": "#V#paper_on_arxiv_2402_18144_c100899e",
                            "file_copy_concept_id": "#V#file_copy_arxiv_2402_18144_c100899e",
                        },
                        {
                            "arxiv_id": "2603.24621",
                            "paper_concept_id": "#V#paper_on_arxiv_2603_24621_eb7a21c4",
                            "file_copy_concept_id": "#V#file_copy_arxiv_2603_24621_eb7a21c4",
                        },
                    ]
                }
            },
        },
        rendered_child_response_text="Represented the requested arXiv papers.",
        child_result_snapshot={
            "iteration_results": [
                {
                    "result_snapshot": {
                        "paper_concept_id": "#V#paper_on_arxiv_2402_18144_c100899e",
                    }
                },
                {
                    "result_snapshot": {
                        "paper_concept_id": "#V#paper_on_arxiv_2603_24621_eb7a21c4",
                    }
                },
            ]
        },
    )

    response_text = outputs["response_text"]
    assert "#V#paper_on_arxiv_2402_18144_c100899e" in response_text
    assert "#V#paper_on_arxiv_2603_24621_eb7a21c4" in response_text
    assert "Paper concept: #V#paper_on_arxiv_2402_18144_c100899e." in response_text
    assert outputs["completion_report"]["surfaceable_concept_ids"] == [
        "#V#paper_on_arxiv_2402_18144_c100899e",
        "#V#file_copy_arxiv_2402_18144_c100899e",
        "#V#paper_on_arxiv_2603_24621_eb7a21c4",
        "#V#file_copy_arxiv_2603_24621_eb7a21c4",
    ]


def test_selected_workflow_outputs_merge_parent_and_child_telemetry() -> None:
    parent_aux = [
        {
            "type": "workflow_dispatch_boundary",
            "boundary": "workflow_handoff",
            "status": "started",
        }
    ]
    child_aux = [
        {
            "type": "tool_call_plan",
            "planned_count": 1,
        }
    ]

    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#tool_calling_workflow",
        child_completed=True,
        final_state="completed",
        failure_detail=None,
        child_outputs={
            "response_text": "Grounded answer.",
            "aux_llm_calls": child_aux,
        },
        rendered_child_response_text="Grounded answer.",
        child_result_snapshot={"response_text": "Grounded answer."},
        parent_aux_llm_calls=parent_aux,
    )

    assert outputs["aux_llm_calls"] == [*parent_aux, *child_aux]


def test_selected_workflow_outputs_promote_child_workflow_episode_trace_ref() -> None:
    parent_aux = [
        {
            "type": "workflow_dispatch_boundary",
            "boundary": "workflow_handoff",
            "status": "started",
            "selected_workflow_id": "#V#example_child_workflow",
        },
        {
            "type": "workflow_use_episode",
            "workflow_id": "#V#example_child_workflow",
            "source": "conversation_turn_selected_workflow",
            "episode_id": "episode-selected",
            "workflow_instance_id": "wf-instance-selected",
            "completed": False,
            "terminal_stage": "#V#workflow_step_selected_failed",
            "final_state": "#V#workflow_step_selected_failed",
            "termination_reason": {
                "code": "failed_terminal_state",
                "detail": "selected_tool_bridge_failed",
            },
        },
    ]

    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#example_child_workflow",
        child_completed=False,
        final_state="#V#workflow_step_selected_failed",
        failure_detail="selected_tool_bridge_failed",
        child_outputs={
            "workflow_execution_summary": {
                "schema_version": "workflow_execution_summary.v1",
                "workflow_id": "#V#example_child_workflow",
                "completed": False,
                "final_state": "#V#workflow_step_selected_failed",
            }
        },
        parent_aux_llm_calls=parent_aux,
    )

    trace = outputs["selected_workflow_trace"]
    assert trace["trace_role"] == "selected_workflow"
    assert trace["workflow_id"] == "#V#example_child_workflow"
    assert trace["workflow_instance_id"] == "wf-instance-selected"
    assert trace["instance_id"] == "wf-instance-selected"
    assert trace["episode_id"] == "episode-selected"
    assert trace["selected_child_trace_source"] == "workflow_use_episode"
    assert outputs["completion_report"]["instance_id"] == "wf-instance-selected"


def test_selected_workflow_outputs_emit_pre_trace_failure_without_trace_ref() -> None:
    parent_aux = [
        {
            "type": "workflow_instance_submission",
            "status": "submission_failed",
            "reason_code": "workflow_definition_not_runnable",
            "workflow_id": "#V#example_selected_workflow",
            "source": "conversation_turn_selected_workflow",
            "error": "Verified durable workflow submission failed",
        }
    ]

    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#example_selected_workflow",
        child_completed=False,
        final_state="selected_workflow_execution_exception",
        failure_detail="workflow_definition_not_runnable",
        child_outputs={},
        parent_aux_llm_calls=parent_aux,
    )

    trace = outputs["selected_workflow_trace"]
    assert trace["trace_role"] == "selected_workflow"
    assert trace["workflow_id"] == "#V#example_selected_workflow"
    assert trace["trace_unavailable"] is True
    assert trace["trace_unavailable_reason"] == "workflow_definition_not_runnable"
    assert trace["dispatch_failure_code"] == "workflow_definition_not_runnable"
    assert trace["selected_workflow_pre_trace_failure"] == {
        "source": "workflow_instance_submission",
        "status": "submission_failed",
        "failure_code": "workflow_definition_not_runnable",
        "failure_detail": "Verified durable workflow submission failed",
        "workflow_id": "#V#example_selected_workflow",
    }
    assert (
        outputs["completion_report"]["trace_unavailable_reason"]
        == "workflow_definition_not_runnable"
    )


def test_selected_workflow_outputs_preserve_execution_trace_id_from_aux_trace() -> None:
    parent_aux = [
        {
            "type": "workflow_execution_trace",
            "workflow_id": "#V#example_child_workflow",
            "execution_trace_id": "exec-trace-selected",
            "instance_id": "wf-instance-selected",
        }
    ]

    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#example_child_workflow",
        child_completed=True,
        final_state="completed",
        failure_detail=None,
        child_outputs={},
        parent_aux_llm_calls=parent_aux,
    )

    trace = outputs["selected_workflow_trace"]
    assert trace["execution_id"] == "exec-trace-selected"
    assert trace["execution_trace_id"] == "exec-trace-selected"
    assert outputs["completion_report"]["execution_id"] == "exec-trace-selected"


def test_selected_workflow_outputs_reconstruct_tool_messages_from_invocations() -> None:
    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#tool_calling_workflow",
        child_completed=True,
        final_state="completed",
        failure_detail=None,
        child_outputs={
            "response_text": "Fetched the latest message.",
            "invocations": [
                {
                    "tool": "gmail_get_message",
                    "status": "ok",
                    "duration_ms": 18,
                    "effective_payload": {
                        "message_id": "msg-123",
                        "subject": "FW: NeurIPS 2026 has received a new review",
                        "labels": ["INBOX", "IMPORTANT"],
                    },
                }
            ],
        },
    )

    tool_messages = outputs["tool_messages"]
    assert tool_messages == [
        {
            "role": "tool",
            "content": '{"tool": "gmail_get_message", "status": "ok", "duration_ms": 18, "payload": {"message_id": "msg-123", "subject": "FW: NeurIPS 2026 has received a new review", "labels": ["INBOX", "IMPORTANT"]}}',
        }
    ]


def test_selected_workflow_outputs_derives_missing_tools_from_turn_contract() -> None:
    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#tool_calling_workflow",
        child_completed=True,
        final_state="completed",
        failure_detail=None,
        child_outputs={
            "response_text": "I found matching concepts.",
            "invocations": [
                {
                    "tool": "search_concepts",
                    "status": "ok",
                    "effective_payload": {
                        "success": True,
                        "results": [{"concept_id": "#V#scientific_paper"}],
                    },
                }
            ],
        },
        rendered_child_response_text="I found matching concepts.",
        turn_expected_outcome_contract={
            "required_tools": ["search_concepts", "fetch_concept"],
        },
    )

    assert outputs["required_prompt_tools"] == [
        "search_concepts",
        "fetch_concept",
    ]
    assert outputs["missing_prompt_tools"] == ["fetch_concept"]
    assert outputs["missing_tool_call_retry_reason_override"] == (
        "turn contract required tool(s) not yet invoked successfully: fetch_concept"
    )
    assert outputs["completion_report"]["required_prompt_tools"] == [
        "search_concepts",
        "fetch_concept",
    ]
    assert outputs["completion_report"]["missing_prompt_tools"] == ["fetch_concept"]
    assert outputs["selected_workflow_trace"]["required_prompt_tools"] == [
        "search_concepts",
        "fetch_concept",
    ]
    assert outputs["selected_workflow_trace"]["missing_prompt_tools"] == [
        "fetch_concept"
    ]


def test_selected_workflow_outputs_derives_missing_tools_from_workflow_contract() -> (
    None
):
    workflow_required_effects_contract = {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "grounded_entity_information_retrieval_evidence",
        "required_effects": [
            {
                "effect_id": "grounded_entity_information_evidence",
                "effect_type": "grounded_evidence",
                "required_tools": [
                    "fetch_concept",
                    "get_text_relations_summary",
                    "get_predicate_incidence",
                    "find_relations_with_argument",
                    "list_uncertain_relationship_assertions",
                ],
                "required_tools_match": "all",
                "recovery_strategies": [
                    {
                        "strategy_id": "recover_text_relations_for_focal_entity",
                        "tool": "get_text_relations_summary",
                        "recovers_tools": ["get_text_relations_summary"],
                        "target_concept_source": "required_fetch_or_focal_concept",
                        "target_concept_argument_name": "concept_id",
                    }
                ],
            }
        ],
    }

    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#entity_information_retrieval_workflow",
        child_completed=True,
        final_state="completed",
        failure_detail=None,
        child_outputs={
            "response_text": "No final user answer was produced.",
            "llm_allowed_tools": [
                "fetch_concept",
                "get_text_relations_summary",
                "get_predicate_incidence",
                "find_relations_with_argument",
                "list_uncertain_relationship_assertions",
            ],
            "invocations": [
                {"tool": "fetch_concept", "status": "ok"},
                {"tool": "get_predicate_incidence", "status": "ok"},
                {"tool": "find_relations_with_argument", "status": "ok"},
                {
                    "tool": "list_uncertain_relationship_assertions",
                    "status": "ok",
                },
            ],
            "workflow_required_effects_contract": workflow_required_effects_contract,
            "workflow_required_effects_contract_source": "definition_metadata",
        },
        rendered_child_response_text="No final user answer was produced.",
    )

    assert outputs["required_prompt_tools"] == [
        "fetch_concept",
        "get_text_relations_summary",
        "get_predicate_incidence",
        "find_relations_with_argument",
        "list_uncertain_relationship_assertions",
    ]
    assert outputs["missing_prompt_tools"] == ["get_text_relations_summary"]
    assert outputs["missing_tool_call_retry_reason_override"] == (
        "workflow required-effect required tool(s) not yet invoked successfully: "
        "get_text_relations_summary"
    )
    assert outputs["workflow_required_effects_contract"] == (
        workflow_required_effects_contract
    )
    assert outputs["completion_report"]["missing_prompt_tools"] == [
        "get_text_relations_summary"
    ]
    assert outputs["selected_workflow_trace"]["missing_prompt_tools"] == [
        "get_text_relations_summary"
    ]


def test_selected_workflow_outputs_exposes_required_step_envelope_as_invocation() -> (
    None
):
    workflow_required_effects_contract = {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "arxiv_paper_representation_readback",
        "required_effects": [
            {
                "effect_id": "arxiv_paper_representation",
                "effect_type": "scholarly_representation",
                "required_tools": ["scholarly_paper.verify_representation"],
            }
        ],
    }

    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#arxiv_paper_representation_workflow",
        child_completed=True,
        final_state="completed",
        failure_detail=None,
        child_outputs={
            "response_text": "Paper represented.",
            "arxiv_id": "2406.15341",
            "workflow_step_result_envelopes": [
                {
                    "workflow_id": "#V#arxiv_paper_representation_workflow",
                    "state_id": "verify_arxiv_path",
                    "action_id": "scholarly_paper.verify_representation",
                    "action_status": "success",
                    "action_outcome": "success",
                    "output_payload": {
                        "scholarly_representation_verified": True,
                        "paper_concept_id": "#V#paper_2406_15341",
                        "file_copy_concept_id": "#V#file_copy_2406_15341",
                    },
                }
            ],
            "workflow_required_effects_contract": workflow_required_effects_contract,
            "workflow_required_effects_contract_source": "definition_metadata",
        },
        rendered_child_response_text="Paper represented.",
    )

    invocations = outputs.get("invocations")
    assert isinstance(invocations, list)
    assert invocations[0]["tool"] == "scholarly_paper.verify_representation"
    assert invocations[0]["workflow_step_evidence"] is True
    assert invocations[0]["effective_payload"]["arxiv_id"] == "2406.15341"
    assert invocations[0]["effective_payload"]["paper_concept_id"] == (
        "#V#paper_2406_15341"
    )
    assert outputs["missing_prompt_tools"] == []


def test_selected_workflow_outputs_credit_resolved_mcp_tool_step_evidence() -> None:
    workflow_required_effects_contract = {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "gmail_arxiv_readback",
        "required_effects": [
            {
                "effect_id": "gmail_arxiv_retrieval",
                "effect_type": "grounded_evidence",
                "required_tools": [
                    "gmail_list_messages",
                    "gmail_get_message",
                    "search_arxiv",
                ],
            }
        ],
    }

    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#zhan_gmail_arxiv_ingestion_workflow",
        child_completed=True,
        final_state="completed",
        failure_detail=None,
        child_outputs={
            "response_text": "Found and represented the new papers.",
            WORKFLOW_STEP_RESULT_ENVELOPES_KEY: [
                {
                    "workflow_id": "#V#zhan_gmail_arxiv_ingestion_workflow",
                    "state_id": "list_messages",
                    "action_id": "workflow_mcp.invoke_tool",
                    "action_status": "success",
                    "action_outcome": "success",
                    "output_payload": {
                        "mcp_requested_tool": "gmail_list_messages",
                        "mcp_resolved_tool": "gmail_list_messages",
                        "success": True,
                        "message_count": 2,
                    },
                },
                {
                    "workflow_id": "#V#zhan_gmail_arxiv_ingestion_workflow",
                    "state_id": "read_message",
                    "action_id": "workflow_mcp.invoke_tool",
                    "action_status": "success",
                    "action_outcome": "success",
                    "output_payload": {
                        "mcp_requested_tool": "gmail_get_message",
                        "mcp_resolved_tool": "gmail_get_message",
                        "success": True,
                        "message_id": "msg-123",
                    },
                },
                {
                    "workflow_id": "#V#zhan_gmail_arxiv_ingestion_workflow",
                    "state_id": "search_papers",
                    "action_id": "workflow_mcp.invoke_tool",
                    "action_status": "success",
                    "action_outcome": "success",
                    "output_payload": {
                        "mcp_requested_tool": "search_arxiv",
                        "mcp_resolved_tool": "search_arxiv",
                        "success": True,
                        "results": [{"arxiv_id": "2402.18144"}],
                    },
                },
            ],
            "workflow_required_effects_contract": workflow_required_effects_contract,
            "workflow_required_effects_contract_source": "definition_metadata",
        },
        rendered_child_response_text="Found and represented the new papers.",
    )

    invocations = outputs.get("invocations")
    assert isinstance(invocations, list)
    assert [item["tool"] for item in invocations] == [
        "gmail_list_messages",
        "gmail_get_message",
        "search_arxiv",
    ]
    assert {item["workflow_action_id"] for item in invocations} == {
        "workflow_mcp.invoke_tool"
    }
    assert outputs["missing_prompt_tools"] == []
    assert outputs["completion_report"]["missing_prompt_tools"] == []


def test_selected_workflow_outputs_filters_turn_contract_tools_to_child_allowed_policy() -> (
    None
):
    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#concept_search_instance_retrieval_workflow",
        child_completed=True,
        final_state="completed",
        failure_detail=None,
        child_outputs={
            "response_text": "Grounded Kobe answer.",
            "llm_allowed_tools": [
                "fetch_concept",
                "get_text_relations_summary",
                "find_relations_with_argument",
                "search_concepts",
            ],
            "required_prompt_tools": ["fetch_concept", "get_related_concepts"],
            "missing_prompt_tools": ["get_related_concepts"],
            "invocations": [
                {"tool": "fetch_concept", "status": "ok"},
                {"tool": "get_text_relations_summary", "status": "ok"},
                {"tool": "find_relations_with_argument", "status": "ok"},
            ],
        },
        rendered_child_response_text="Grounded Kobe answer.",
        turn_expected_outcome_contract={
            "required_tools": ["fetch_concept", "get_related_concepts"],
        },
    )

    assert outputs["required_prompt_tools"] == ["fetch_concept"]
    assert outputs["turn_expected_required_tools"] == [
        "fetch_concept",
        "get_related_concepts",
    ]
    assert outputs["missing_prompt_tools"] == []
    assert (
        "get_related_concepts"
        not in outputs["completion_report"]["required_prompt_tools"]
    )


def test_concept_profile_wrong_target_evidence_blocks_completion() -> None:
    workflow_required_effects_contract = {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "grounded_concept_profile_retrieval_evidence",
        "required_effects": [
            {
                "effect_id": "grounded_concept_profile_evidence",
                "effect_type": "grounded_evidence",
                "required_tools": [
                    "fetch_concept",
                    "get_text_relations_summary",
                    "find_relations_with_argument",
                ],
                "required_tools_match": "all",
                "missing_failure_code": "concept_profile_evidence_missing",
                "wrong_target_failure_code": "concept_profile_evidence_wrong_target",
            }
        ],
    }
    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#concept_search_instance_retrieval_workflow",
        child_completed=True,
        final_state="completed",
        failure_detail=None,
        child_outputs={
            "response_text": "Michael Witbrock is represented as a person.",
            "final_response": "Michael Witbrock is represented as a person.",
            "invocations": [
                {
                    "tool": "fetch_concept",
                    "status": "ok",
                    "effective_arguments": {"concept_id": "#V#michael_witbrock"},
                    "effective_payload": {
                        "success": True,
                        "concept_id": "#V#michael_witbrock",
                    },
                },
                {
                    "tool": "get_text_relations_summary",
                    "status": "ok",
                    "effective_arguments": {"concept_id": "#V#michael_witbrock"},
                    "effective_payload": {
                        "success": True,
                        "concept_id": "#V#michael_witbrock",
                    },
                },
                {
                    "tool": "find_relations_with_argument",
                    "status": "ok",
                    "effective_arguments": {"concept_id": "#V#michael_witbrock"},
                    "effective_payload": {
                        "success": True,
                        "concept_id": "#V#michael_witbrock",
                    },
                },
            ],
            "required_prompt_tools": [
                "fetch_concept",
                "get_text_relations_summary",
                "find_relations_with_argument",
            ],
            "workflow_required_effects_contract": workflow_required_effects_contract,
            "workflow_required_effects_contract_source": "definition_metadata",
        },
        rendered_child_response_text="Michael Witbrock is represented as a person.",
        child_result_snapshot={
            "response_text": "Michael Witbrock is represented as a person."
        },
        turn_expected_outcome_contract={
            "schema_version": "turn_expected_outcome_contract.v1",
            "fields": {"summary": "Tell me about #V#kobe_knowles."},
            "required_tools": [
                "fetch_concept",
                "get_text_relations_summary",
                "find_relations_with_argument",
            ],
            "target_concept_ids": ["#V#kobe_knowles"],
        },
    )

    record = build_turn_execution_record(
        request_id="req-wrong-target",
        session_id="session-1",
        namespace="#V#test_namespace",
        actor_concept_id="#V#michael_witbrock",
        user_id="#V#michael_witbrock",
        org_id="#V#university_of_auckland_strong_ai_lab",
        prompt_text="tell me about #V#kobe_knowles",
        response_text=outputs["final_response"],
        interaction_timestamp_utc="2026-04-24T08:00:00Z",
        workflow_routing={
            "workflow_id": "#V#concept_search_instance_retrieval_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        tool_invocations=outputs["invocations"],
        selected_workflow_trace=outputs["selected_workflow_trace"],
        turn_expected_outcome_contract=outputs["turn_expected_outcome_contract_state"],
        completion_report=outputs["completion_report"],
        required_prompt_tools=outputs["required_prompt_tools"],
    )

    gate = record["completion_gate"]
    assert gate["safe_to_claim_completion"] is False
    assert "concept_profile_evidence_wrong_target" in (
        gate.get("blocking_failure_codes") or []
    )
    effect_by_id = {
        effect["effect_id"]: effect for effect in record["required_effects"]
    }
    workflow_effect = effect_by_id["grounded_concept_profile_evidence"]
    assert workflow_effect["status"] == "not_executed"
    assert workflow_effect["targets"] == ["#V#kobe_knowles"]
    assert workflow_effect["failure_code"] == "concept_profile_evidence_wrong_target"
    prompt_fetch_effect = effect_by_id[
        "effect_prompt_required_evidence_fetch_concept_1"
    ]
    assert prompt_fetch_effect["targets"] == ["#V#kobe_knowles"]
    assert prompt_fetch_effect["failure_code"] == (
        "prompt_required_evidence_fetch_concept_wrong_target"
    )
    assert record["execution"]["summary"]["required_evidence_target_concept_ids"] == [
        "#V#kobe_knowles"
    ]


def _unsafe_missing_grounded_evidence_record() -> dict[str, Any]:
    return {
        "completion_gate": {
            "decision": "escalation_required",
            "decision_reason": (
                "Required grounded entity-information evidence was not retrieved."
            ),
            "safe_to_claim_completion": False,
            "requires_follow_up": True,
            "blocking_effect_ids": ["grounded_entity_information_evidence"],
            "blocking_failure_codes": ["entity_information_evidence_missing"],
            "evidence_payload": {
                "unresolved_preconditions": [
                    {
                        "effect_id": "grounded_entity_information_evidence",
                        "effect_type": "grounded_evidence",
                        "status": "not_executed",
                        "status_reason": (
                            "Required grounded entity-information evidence was not "
                            "retrieved."
                        ),
                        "failure_codes": ["entity_information_evidence_missing"],
                    }
                ]
            },
        },
        "required_effects": [
            {
                "effect_id": "grounded_entity_information_evidence",
                "effect_type": "grounded_evidence",
                "status": "not_executed",
                "status_reason": (
                    "Required grounded entity-information evidence was not retrieved."
                ),
                "failure_code": "entity_information_evidence_missing",
            }
        ],
    }


def test_selected_workflow_renderer_blocks_false_empty_answer_when_required_effects_unresolved() -> (
    None
):
    response = render_selected_workflow_user_response(
        selected_workflow_id="#V#entity_information_retrieval_workflow",
        child_completed=True,
        final_state="#V#workflow_step_entity_information_retrieval_workflow_completed",
        failure_detail=None,
        child_outputs={
            "final_response": (
                "I could not find any information regarding your identity, and no "
                "papers were found associated with you."
            ),
            "turn_execution_record": _unsafe_missing_grounded_evidence_record(),
        },
        child_result_snapshot=None,
    )

    assert isinstance(response, str)
    assert response.startswith(
        "Execution status: required grounded evidence was not retrieved."
    )
    assert "entity_information_evidence_missing" in response
    assert "could not find any information regarding your identity" not in response


def test_custom_workflow_response_renderer_blocks_false_empty_answer_when_gate_is_unsafe() -> (
    None
):
    response = InternalMCPChatOrchestrator._render_custom_workflow_response_text(
        workflow_id="#V#entity_information_retrieval_workflow",
        workflow_result=SimpleNamespace(
            completed=True,
            final_state="#V#workflow_step_entity_information_retrieval_workflow_completed",
            error=None,
            data={
                "response_text": (
                    "I could not find any information regarding your identity, and "
                    "no papers were found associated with you."
                ),
                "turn_execution_record": _unsafe_missing_grounded_evidence_record(),
            },
        ),
    )

    assert isinstance(response, str)
    assert response.startswith(
        "Execution status: required grounded evidence was not retrieved."
    )
    assert "entity_information_evidence_missing" in response
    assert "no papers were found associated with you" not in response


def test_selected_workflow_renderer_preserves_grounded_success_response() -> None:
    response = render_selected_workflow_user_response(
        selected_workflow_id="#V#entity_information_retrieval_workflow",
        child_completed=True,
        final_state="#V#workflow_step_entity_information_retrieval_workflow_completed",
        failure_detail=None,
        child_outputs={
            "final_response": (
                "You are Michael Witbrock. Grounded papers: Learning to Tell Two "
                "Spirals Apart."
            ),
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "completed",
                    "safe_to_claim_completion": True,
                    "requires_follow_up": False,
                },
                "required_effects": [
                    {
                        "effect_id": "grounded_entity_information_evidence",
                        "effect_type": "grounded_evidence",
                        "status": "satisfied",
                    }
                ],
            },
        },
        child_result_snapshot=None,
    )

    assert response == (
        "You are Michael Witbrock. Grounded papers: Learning to Tell Two Spirals Apart."
    )


def test_selected_workflow_renderer_preserves_subworkflow_result_response() -> None:
    response = render_selected_workflow_user_response(
        selected_workflow_id="#V#general_mail_review_workflow",
        child_completed=True,
        final_state="completed",
        failure_detail=None,
        child_outputs={
            "result": {
                "response_text": "Here is the requested mail review table.",
            }
        },
        child_result_snapshot=None,
    )

    assert response == "Here is the requested mail review table."
