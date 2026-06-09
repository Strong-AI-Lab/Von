from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from src.backend.services.turn_execution_record_service import (
    build_turn_execution_record,
)
from src.backend.services.required_tool_obligation_service import (
    BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED,
    BLOCKER_TARGET_REQUIRED_TOOL_ATTEMPT_FAILED,
)
from src.backend.workflows.durable.turn_execution_runtime_support import (
    run_turn_execution_completion_gate,
    run_turn_execution_critic,
)

_CREATE_CONCEPTS_PARENT_ERROR = (
    "create_concepts: Missing required field 'parent_id' for create_concepts input: "
    "parent_id (str, parent concept_id), concepts (list of {name, kind?, "
    "description?, notes?})."
)

_KR_REQUIRED_TOOLS = [
    "create_concepts",
    "add_relationship",
    "upsert_singleton_text_relation",
    "fetch_concept",
    "get_text_relations_summary",
]


def _validation_failure_aux() -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_contract_attempt",
            "schema_version": "tool_contract_attempt.v1",
            "stage": "tool_calling.validate",
            "tool_calls": [
                {
                    "tool": "create_concepts",
                    "payload": {
                        "concepts": [
                            {"name": "Assistant label", "kind": "type"},
                        ],
                        "namespace": "#V#user",
                    },
                }
            ],
            "validation_error_count": 1,
            "validation_errors": [_CREATE_CONCEPTS_PARENT_ERROR],
            "validation_warnings": [],
            "repair_attempted": True,
            "repair_succeeded": None,
            "diagnostics": [
                {
                    "schema_version": "tool_call_contract_validation.v1",
                    "status": "invalid",
                    "tool": "create_concepts",
                    "error_code": "schema_validation_failed",
                    "message": _CREATE_CONCEPTS_PARENT_ERROR,
                    "payload": {
                        "concepts": [
                            {"name": "Assistant label", "kind": "type"},
                        ],
                        "namespace": "#V#user",
                    },
                }
            ],
        },
        {
            "type": "tool_call_validation_repair_result",
            "stage": "tool_calling.repair",
            "attempt": 1,
            "budget": 1,
            "succeeded": False,
            "repaired_call_count": 0,
            "decision": {
                "schema_version": "tool_call_repair_decision.v1",
                "required": False,
                "attempted": True,
                "succeeded": False,
                "attempts": 1,
                "budget": 1,
                "remaining": 0,
                "error_count": 1,
                "reason": "repair_failed_or_returned_empty",
            },
        },
    ]


def _build_record(*, required_prompt_tools: list[str]) -> dict[str, Any]:
    return build_turn_execution_record(
        request_id="req-required-tool-validation",
        session_id="session-required-tool-validation",
        namespace="#V#user@org",
        actor_concept_id="#V#user",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Create the represented ontology artefacts.",
        response_text="Tool validation failed.",
        interaction_timestamp_utc="2026-05-02T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        tool_invocations=[],
        aux_llm_calls=_validation_failure_aux(),
        required_prompt_tools=required_prompt_tools,
    )


def _build_kr_label_record(
    *,
    required_prompt_tools: list[str],
    tool_invocations: list[dict[str, Any]],
) -> dict[str, Any]:
    return build_turn_execution_record(
        request_id="req-represented-label-contract",
        session_id="session-represented-label-contract",
        namespace="#V#user@org",
        actor_concept_id="#V#user",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text=(
            "Create represented workflow labels so Von can avoid repeating "
            "completed KB work, then use the API to create any useful ones."
        ),
        response_text="The requested tool evidence is present.",
        interaction_timestamp_utc="2026-05-02T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        tool_invocations=tool_invocations,
        aux_llm_calls=[],
        required_prompt_tools=required_prompt_tools,
    )


def _kr_write_invocations() -> list[dict[str, Any]]:
    return [
        {
            "tool": "create_concepts",
            "status": "ok",
            "effective_arguments": {
                "parent_id": "#V#workflow_marker",
                "concepts": [{"name": "Already scanned marker"}],
            },
            "effective_payload": {
                "success": True,
                "concept_id": "#V#already_scanned_marker",
            },
        },
        {
            "tool": "add_relationship",
            "status": "ok",
            "effective_arguments": {
                "source_id": "#V#already_scanned_marker",
                "predicate": "#V#hasWorkflow",
                "target": "#V#paper_ingestion_workflow",
            },
            "effective_payload": {"success": True},
        },
        {
            "tool": "upsert_singleton_text_relation",
            "status": "ok",
            "effective_arguments": {
                "concept_id": "#V#already_scanned_marker",
                "predicate_concept_id": "#V#hasDescription",
                "text": "Marker used to avoid repeating completed workflow work.",
            },
            "effective_payload": {
                "success": True,
                "concept_id": "#V#already_scanned_marker",
            },
        },
    ]


def _kr_readback_invocations() -> list[dict[str, Any]]:
    return [
        {
            "tool": "fetch_concept",
            "status": "ok",
            "effective_arguments": {"concept_id": "#V#already_scanned_marker"},
            "effective_payload": {
                "success": True,
                "concept_id": "#V#already_scanned_marker",
            },
        },
        {
            "tool": "get_text_relations_summary",
            "status": "ok",
            "effective_arguments": {"concept_id": "#V#already_scanned_marker"},
            "effective_payload": {
                "success": True,
                "concept_id": "#V#already_scanned_marker",
            },
        },
    ]


def test_turn_record_marks_required_tool_schema_failure_as_unresolved_effect() -> None:
    record = _build_record(
        required_prompt_tools=["create_concepts", "add_relationship"],
    )

    effect_by_id = {
        effect["effect_id"]: effect for effect in record["required_effects"]
    }
    create_effect = effect_by_id["effect_prompt_required_mutation_create_concepts_1"]
    assert create_effect["status"] == "not_satisfied"
    assert create_effect["status_reason"] == _CREATE_CONCEPTS_PARENT_ERROR
    assert create_effect["failure_code"] == (
        "prompt_required_mutation_create_concepts_failed"
    )
    assert "schema_validation_failed" in create_effect["failure_codes"]
    assert "tool_call_validation_failed" in create_effect["failure_codes"]
    assert create_effect["tool_call_repair_outcome"] == "repair_failed"
    assert create_effect["tool_call_repair_stop_reason"] == (
        "repair_failed_or_returned_empty"
    )

    relationship_effect = effect_by_id[
        "effect_prompt_required_mutation_add_relationship_2"
    ]
    assert relationship_effect["status"] == "not_executed"

    gate = record["completion_gate"]
    assert gate["safe_to_claim_completion"] is False
    assert gate["requires_follow_up"] is True
    assert gate["evidence_payload"]["required_effect_count"] == 3
    assert "schema_validation_failed" in gate["blocking_failure_codes"]
    assert BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED in gate["blocking_failure_codes"]

    summary = record["execution"]["summary"]
    ledger = summary["required_tool_obligations"]
    create_obligation = next(
        obligation
        for obligation in ledger["obligations"]
        if obligation["tool_name"] == "create_concepts"
    )
    assert create_obligation["blocking_reason"] == (
        BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED
    )
    assert create_obligation["last_attempt_status"] == "schema_validation_failed"
    assert create_obligation["last_attempt_message"] == _CREATE_CONCEPTS_PARENT_ERROR
    assert create_obligation["tool_call_validation_errors"][0]["message"] == (
        _CREATE_CONCEPTS_PARENT_ERROR
    )
    assert BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED in (
        summary["required_tool_obligation_blocking_failure_codes"]
    )

    dispatch = record["workflow_routing_diagnostics"]["dispatch"]
    assert BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED in (
        dispatch["required_tool_obligation_blocking_failure_codes"]
    )


def test_completion_gate_rebuilds_stale_zero_effect_record_with_required_tools() -> (
    None
):
    data: dict[str, Any] = {
        "turn_execution_record": {
            "required_effects": [],
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "evidence_payload": {"required_effect_count": 0},
            },
        },
        "required_prompt_tools": ["create_concepts"],
        "prompt": "Create the represented ontology artefacts.",
        "final_response": "Done.",
        "current_response": "Done.",
        "invocations": [],
        "aux_llm_calls": _validation_failure_aux(),
        "workflow_routing": {
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        "turn_id": "req-stale-gate",
        "conversation_session_id": "session-stale-gate",
        "user_concept_id": "#V#user",
        "org_concept_id": "#V#org",
        "turn_execution_record_generated_at": "2026-05-02T00:00:00Z",
    }
    request = SimpleNamespace(
        data=data,
        inputs={},
        environment=SimpleNamespace(user_namespace="#V#user@org"),
    )

    result = run_turn_execution_completion_gate(
        request,
        annotation_component="test",
        annotation_function="test_completion_gate",
        introspection_auto_apply_env="VON_TEST_UNUSED",
    )

    assert result.outputs["completion_gate_safe_to_claim_completion"] is False
    assert result.outputs["completion_gate_requires_follow_up"] is True
    assert (
        result.outputs["completion_gate_evidence_payload"]["required_effect_count"] == 2
    )
    assert (
        "schema_validation_failed"
        in result.outputs["completion_gate_blocking_failure_codes"]
    )
    assert BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED in (
        result.outputs["completion_gate_blocking_failure_codes"]
    )
    assert any(
        entry.get("type") == "completion_gate_record_rebuilt"
        for entry in data["aux_llm_calls"]
    )


def test_completion_gate_rebuilds_stale_required_tool_obligation_effect() -> None:
    data: dict[str, Any] = {
        "turn_execution_record": {
            "required_effects": [
                {
                    "effect_id": "effect_required_tool_obligations_1",
                    "intent_origin": "required_tool_obligation_ledger",
                    "effect_type": "tool_execution",
                    "required_tools": ["scholarly_paper.verify_representation"],
                    "status": "not_executed",
                    "status_reason": (
                        "Required tool obligations were not satisfied: "
                        "scholarly_paper.verify_representation"
                    ),
                    "failure_code": "required_tool_not_available_on_gateway",
                    "failure_codes": ["required_tool_not_available_on_gateway"],
                }
            ],
            "completion_gate": {
                "decision": "escalation_required",
                "decision_reason": "Required tool execution was not observed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_required_tool_obligations_1"],
                "blocking_failure_codes": ["required_tool_not_available_on_gateway"],
                "evidence_payload": {
                    "required_effect_count": 1,
                    "unresolved_preconditions": [
                        {
                            "effect_id": "effect_required_tool_obligations_1",
                            "effect_type": "tool_execution",
                            "status": "not_executed",
                            "status_reason": (
                                "Required tool obligations were not satisfied: "
                                "scholarly_paper.verify_representation"
                            ),
                            "failure_codes": [
                                "required_tool_not_available_on_gateway"
                            ],
                        }
                    ],
                },
            },
        },
        "required_prompt_tools": ["scholarly_paper.verify_representation"],
        "prompt": "Represent this paper: https://arxiv.org/abs/2106.03245",
        "final_response": "The paper has been successfully represented.",
        "current_response": "The paper has been successfully represented.",
        "invocations": [
            {
                "tool": "scholarly_paper.verify_representation",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "arxiv_id": "2106.03245",
                    "scholarly_representation_verified": True,
                    "paper_concept_id": "#V#paper_2106_03245",
                },
            }
        ],
        "aux_llm_calls": [],
        "workflow_routing": {
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        "turn_expected_outcome_contract_state": {
            "schema_version": "turn_expected_outcome_contract.v1",
            "fields": {"summary": "Represent arXiv:2106.03245."},
            "required_tools": ["scholarly_paper.verify_representation"],
        },
        "turn_id": "req-stale-required-tool-effect",
        "conversation_session_id": "session-stale-required-tool-effect",
        "user_concept_id": "#V#user",
        "org_concept_id": "#V#org",
        "turn_execution_record_generated_at": "2026-05-02T00:00:00Z",
    }
    request = SimpleNamespace(
        data=data,
        inputs={},
        environment=SimpleNamespace(user_namespace="#V#user@org"),
    )

    result = run_turn_execution_completion_gate(
        request,
        annotation_component="test",
        annotation_function="test_completion_gate",
        introspection_auto_apply_env="VON_TEST_UNUSED",
    )

    assert result.outputs["completion_gate_safe_to_claim_completion"] is True
    assert result.outputs["completion_gate_requires_follow_up"] is False
    assert result.outputs["completion_gate_blocking_failure_codes"] == []
    assert any(
        entry.get("type") == "completion_gate_record_rebuilt"
        and entry.get("reason")
        == "stale_required_tool_obligation_effect_satisfied_by_current_invocations"
        for entry in data["aux_llm_calls"]
    )


def test_completion_gate_promotes_selected_workflow_response_to_response_text() -> None:
    data = {
        "turn_execution_record": {
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
            },
            "required_effects": [],
        },
        "selected_workflow_user_response": "Grounded selected workflow answer.",
        "final_response": "Generic narration answer.",
        "response_text": "",
        "current_response": "",
        "invocations": [],
        "aux_llm_calls": [],
    }
    request = SimpleNamespace(
        data=data,
        inputs={},
        environment=SimpleNamespace(user_namespace="#V#user@org"),
    )

    result = run_turn_execution_completion_gate(
        request,
        annotation_component="test",
        annotation_function="test_completion_gate",
        introspection_auto_apply_env="VON_TEST_UNUSED",
    )

    assert result.outputs["completion_gate_safe_to_claim_completion"] is True
    assert result.outputs["completion_gate_requires_follow_up"] is False
    assert result.outputs["selected_workflow_user_response"] == (
        "Grounded selected workflow answer."
    )
    assert result.outputs["final_response"] == "Grounded selected workflow answer."
    assert result.outputs["response_text"] == "Grounded selected workflow answer."
    assert result.outputs["current_response"] == "Grounded selected workflow answer."


def test_postcondition_critic_bundle_carries_final_answer_projection_for_visible_answer_check() -> (
    None
):
    projected_tool_payload = {
        "tool": "gmail_list_messages",
        "status": "ok",
        "call_id": "gmail-list-1",
        "payload": {
            "messages": [
                {
                    "message_id": "msg-1",
                    "sender": "sender@example.test",
                    "subject": "Lab scheduling",
                    "date": "2026-06-06",
                    "snippet": "Labels: Work, Lab",
                }
            ],
            "_tool_evidence_projection": {
                "tool_concept_id": "#V#gmail_list_messages_tool",
                "evidence_view_concept_ids": [
                    "#V#gmail_message_final_answer_evidence_view"
                ],
                "preserved_fields": [
                    {
                        "field_concept_id": "#V#gmail_sender_field",
                        "output_key": "sender",
                    },
                    {
                        "field_concept_id": "#V#gmail_subject_field",
                        "output_key": "subject",
                    },
                    {
                        "field_concept_id": "#V#gmail_date_field",
                        "output_key": "date",
                    },
                    {
                        "field_concept_id": "#V#gmail_snippet_field",
                        "output_key": "snippet",
                    },
                ],
                "missing_required_fields": [],
                "omitted_fields": [],
                "redacted_fields": [],
            },
        },
    }
    data = {
        "turn_id": "req-gmail-projection-summary-only",
        "conversation_session_id": "session-gmail-projection-summary-only",
        "prompt": "List the Gmail messages and their labels.",
        "final_response": "Execution status: processed the Gmail request successfully.",
        "current_response": "Execution status: processed the Gmail request successfully.",
        "response_text": "Execution status: processed the Gmail request successfully.",
        "workflow_routing": {
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_calling",
            "source": "selector",
        },
        "invocations": [
            {
                "tool": "gmail_list_messages",
                "status": "ok",
                "effective_payload": projected_tool_payload["payload"],
            }
        ],
        "aux_llm_calls": [
            {
                "type": "workflow_model_policy_stage",
                "stage": "summariser",
                "workflow_stage_id": "final_answer",
                "request": {
                    "prompt": "Compose the final answer.",
                    "context_messages": [
                        {"role": "tool", "content": json.dumps(projected_tool_payload)}
                    ],
                },
            }
        ],
    }
    request = SimpleNamespace(
        data=data,
        inputs={"emit_default_critic_verdict": False},
        environment=SimpleNamespace(user_namespace="#V#user@org"),
    )

    result = run_turn_execution_critic(
        request,
        annotation_component="test",
        annotation_function="test_critic",
    )

    bundle = result.outputs["turn_execution_critic_evidence_bundle"]
    synthesis = bundle["final_answer_synthesis"]
    projection = synthesis["tool_evidence_projection"]
    assert projection["projection_count"] == 1
    assert projection["entries"][0]["projected_payload"]["messages"][0][
        "subject"
    ] == "Lab scheduling"
    assert "#V#gmail_subject_field" in projection["preserved_field_concept_ids"]


def test_completion_gate_blocks_operational_summary_when_authoritative_critic_flags_projected_evidence_mismatch() -> (
    None
):
    summary_only_answer = "Execution status: processed the Gmail request successfully."
    data = {
        "turn_id": "req-gmail-summary-only-blocked",
        "conversation_session_id": "session-gmail-summary-only-blocked",
        "prompt": "List the Gmail messages and their labels.",
        "final_response": summary_only_answer,
        "current_response": summary_only_answer,
        "response_text": summary_only_answer,
        "workflow_routing": {
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_calling",
            "source": "selector",
        },
        "invocations": [
            {
                "tool": "gmail_list_messages",
                "status": "ok",
                "effective_payload": {
                    "messages": [
                        {
                            "message_id": "msg-1",
                            "sender": "sender@example.test",
                            "subject": "Lab scheduling",
                            "date": "2026-06-06",
                            "snippet": "Labels: Work, Lab",
                        }
                    ]
                },
            }
        ],
        "critic_verdict": {
            "workflow_id": "#V#kb_mutation_postcondition_critic_workflow",
            "verdict": "follow_up_required",
            "confidence": 0.92,
            "assessment_summary": (
                "Projected final-answer evidence contained concrete message "
                "fields, but the visible answer only reported operational status."
            ),
            "required_evidence_answer_consistency_blocker": {
                "effect_type": "required_evidence_answer_consistency",
                "status": "not_satisfied",
                "decision": "partial",
                "decision_reason": (
                    "Projected final-answer evidence was available, but the "
                    "visible answer did not consume it."
                ),
                "status_reason": (
                    "The answer is an operational summary rather than a grounded "
                    "message-list answer."
                ),
                "failure_code": "projected_final_answer_evidence_not_consumed",
                "failure_codes": [
                    "projected_final_answer_evidence_not_consumed"
                ],
                "repeat_eligible": True,
                "blocker_source": "critic_verdict",
            },
            "recommendations": [],
        },
        "aux_llm_calls": [],
    }
    request = SimpleNamespace(
        data=data,
        inputs={},
        environment=SimpleNamespace(user_namespace="#V#user@org"),
    )

    result = run_turn_execution_completion_gate(
        request,
        annotation_component="test",
        annotation_function="test_completion_gate",
        introspection_auto_apply_env="VON_TEST_UNUSED",
    )

    assert result.outputs["completion_gate_safe_to_claim_completion"] is False
    assert result.outputs["completion_gate_requires_follow_up"] is True
    assert result.outputs["completion_gate_decision"] == "partial"
    assert "projected_final_answer_evidence_not_consumed" in (
        result.outputs["completion_gate_blocking_failure_codes"]
    )
    assert result.outputs["final_response"].startswith(
        "Execution status: required grounded evidence was not retrieved."
    )
    assert (
        "projected_final_answer_evidence_not_consumed"
        in result.outputs["final_response"]
    )
    evidence_payload = result.outputs["completion_gate_evidence_payload"]
    blocker = evidence_payload["required_evidence_answer_consistency_blocker"]
    assert blocker["blocker_source"] == "critic_verdict"


def test_kr_required_tools_block_completion_when_write_and_readback_are_absent() -> (
    None
):
    record = _build_kr_label_record(
        required_prompt_tools=_KR_REQUIRED_TOOLS,
        tool_invocations=[],
    )

    effect_by_id = {
        effect["effect_id"]: effect for effect in record["required_effects"]
    }

    assert (
        effect_by_id["effect_prompt_required_mutation_create_concepts_1"]["status"]
        == "not_executed"
    )
    assert (
        effect_by_id["effect_prompt_required_mutation_add_relationship_2"]["status"]
        == "not_executed"
    )
    assert (
        effect_by_id[
            "effect_prompt_required_mutation_upsert_singleton_text_relation_3"
        ]["status"]
        == "not_executed"
    )
    assert (
        effect_by_id["effect_prompt_required_evidence_fetch_concept_1"]["status"]
        == "not_executed"
    )
    assert (
        effect_by_id["effect_prompt_required_evidence_get_text_relations_summary_2"][
            "status"
        ]
        == "not_executed"
    )
    assert record["completion_gate"]["safe_to_claim_completion"] is False
    assert "prompt_required_mutation_create_concepts_missing" in (
        record["completion_gate"]["blocking_failure_codes"]
    )
    assert "prompt_required_mutation_add_relationship_missing" in (
        record["completion_gate"]["blocking_failure_codes"]
    )
    assert "prompt_required_mutation_upsert_singleton_text_relation_missing" in (
        record["completion_gate"]["blocking_failure_codes"]
    )


def test_kr_required_tools_block_completion_when_write_lacks_readback() -> None:
    record = _build_kr_label_record(
        required_prompt_tools=_KR_REQUIRED_TOOLS,
        tool_invocations=_kr_write_invocations(),
    )

    effect_by_id = {
        effect["effect_id"]: effect for effect in record["required_effects"]
    }
    assert (
        effect_by_id["effect_prompt_required_mutation_create_concepts_1"]["status"]
        == "satisfied"
    )
    assert (
        effect_by_id["effect_prompt_required_mutation_add_relationship_2"]["status"]
        == "satisfied"
    )
    assert (
        effect_by_id[
            "effect_prompt_required_mutation_upsert_singleton_text_relation_3"
        ]["status"]
        == "satisfied"
    )

    gate = record["completion_gate"]
    assert gate["safe_to_claim_completion"] is False
    assert "prompt_required_evidence_fetch_concept_missing" in (
        gate["blocking_failure_codes"]
    )
    assert "prompt_required_evidence_get_text_relations_summary_missing" in (
        gate["blocking_failure_codes"]
    )
    assert any(
        check.get("effect_id") == "mutation_3"
        and check.get("verification_mode") == "state_requery_missing"
        for check in record["postcondition_checks"]
    )


def test_kr_required_tools_allow_completion_after_write_and_readback() -> None:
    record = _build_kr_label_record(
        required_prompt_tools=_KR_REQUIRED_TOOLS,
        tool_invocations=[
            *_kr_write_invocations(),
            *_kr_readback_invocations(),
        ],
    )

    assert record["completion_gate"]["safe_to_claim_completion"] is True
    assert record["completion_gate"]["requires_follow_up"] is False
    effect_by_id = {
        effect["effect_id"]: effect for effect in record["required_effects"]
    }
    assert (
        effect_by_id["effect_prompt_required_mutation_create_concepts_1"]["status"]
        == "satisfied"
    )
    assert (
        effect_by_id["effect_prompt_required_mutation_add_relationship_2"]["status"]
        == "satisfied"
    )
    assert (
        effect_by_id[
            "effect_prompt_required_mutation_upsert_singleton_text_relation_3"
        ]["status"]
        == "satisfied"
    )
    assert any(
        check.get("effect_id") == "mutation_3"
        and check.get("verification_mode") == "state_requery_observed"
        and check.get("status") == "verified"
        for check in record["postcondition_checks"]
    )


def test_contract_required_gmail_profile_and_relation_tools_block_generic_chat() -> (
    None
):
    record = build_turn_execution_record(
        request_id="req-gmail-predicate-required-evidence",
        session_id="session-gmail-predicate-required-evidence",
        namespace="#V#user@org",
        actor_concept_id="#V#user",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text=(
            "What gmail profiles can you see, and let me know if any are connected "
            "to me via predicates"
        ),
        response_text="#V#michael_witbrock",
        interaction_timestamp_utc="2026-05-10T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#chat_assistant_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        tool_invocations=[],
        turn_expected_outcome_contract={
            "required_tools": [
                "gmail_list_profiles",
                "find_relations_with_argument",
            ]
        },
    )

    gate = record["completion_gate"]
    assert gate["safe_to_claim_completion"] is False
    assert "prompt_required_evidence_gmail_list_profiles_missing" in (
        gate["blocking_failure_codes"]
    )
    assert "prompt_required_evidence_find_relations_with_argument_missing" in (
        gate["blocking_failure_codes"]
    )
    summary = record["execution"]["summary"]
    assert summary["required_tool_obligations"]["unsatisfied_required_tools"] == [
        "gmail_list_profiles",
        "find_relations_with_argument",
    ]


def test_target_failed_required_reads_are_not_closed_by_later_other_target_success() -> (
    None
):
    required_tools = [
        "fetch_concept",
        "find_relations_with_argument",
        "get_text_relations_summary",
    ]
    failed_targets = [
        "#V#gmail_message_19e2b80d1bb1cf41",
        "#V#gmail_message_19e0ae6e60af70e8",
        "#V#gmail_message_19dfbb8ec1f7bdd8",
    ]
    tool_invocations: list[dict[str, Any]] = []
    for target in failed_targets:
        for tool_name in required_tools:
            tool_invocations.append(
                {
                    "tool": tool_name,
                    "status": "error",
                    "arguments": {"concept_id": target},
                    "payload": {
                        "success": False,
                        "concept_id": target,
                        "error": (
                            "User cannot view text for an inaccessible concept"
                            if tool_name == "get_text_relations_summary"
                            else "Concept not found or inaccessible"
                        ),
                    },
                }
            )
    for tool_name in required_tools:
        tool_invocations.append(
            {
                "tool": tool_name,
                "status": "ok",
                "arguments": {"concept_id": "#V#michael_witbrock"},
                "payload": {"success": True, "concept_id": "#V#michael_witbrock"},
            }
        )

    record = build_turn_execution_record(
        request_id="req-target-specific-required-readback",
        session_id="session-target-specific-required-readback",
        namespace="#V#user@org",
        actor_concept_id="#V#user",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Check that the recently represented items are now represented.",
        response_text="The requested read-back is complete.",
        interaction_timestamp_utc="2026-05-16T02:16:56Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        tool_invocations=tool_invocations,
        turn_expected_outcome_contract={
            "summary": (
                "Verify that recently found items are represented in Vontology."
            ),
            "required_tools": required_tools,
        },
    )

    gate = record["completion_gate"]
    assert gate["safe_to_claim_completion"] is False
    assert gate["requires_follow_up"] is True
    assert BLOCKER_TARGET_REQUIRED_TOOL_ATTEMPT_FAILED in (
        gate["blocking_failure_codes"]
    )

    summary = record["execution"]["summary"]
    ledger = summary["required_tool_obligations"]
    assert ledger["unsatisfied_required_tools"] == required_tools
    assert BLOCKER_TARGET_REQUIRED_TOOL_ATTEMPT_FAILED in (
        summary["required_tool_obligation_blocking_failure_codes"]
    )
    fetch_obligation = next(
        item for item in ledger["obligations"] if item["tool_name"] == "fetch_concept"
    )
    assert fetch_obligation["target_closure"]["unresolved_failed_target_count"] == 3
    assert fetch_obligation["target_closure"]["successful_target_count"] == 1
    assert record["workflow_routing_diagnostics"]["dispatch"][
        "required_tool_obligation_blocking_failure_codes"
    ] == [BLOCKER_TARGET_REQUIRED_TOOL_ATTEMPT_FAILED]
