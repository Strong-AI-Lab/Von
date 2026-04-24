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
        "You are Michael Witbrock. Grounded papers: Learning to Tell Two "
        "Spirals Apart."
    )
