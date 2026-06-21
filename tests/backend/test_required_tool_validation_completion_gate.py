from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from src.backend.services.turn_execution_record_service import (
    build_turn_execution_record,
    _derive_zero_tool_execution_reason,
)
from src.backend.services.required_tool_obligation_service import (
    BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED,
    BLOCKER_TARGET_REQUIRED_TOOL_ATTEMPT_FAILED,
)
from src.backend.workflows.durable.turn_execution_runtime_support import (
    build_turn_execution_selected_workflow_outputs,
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


def test_tool_route_zero_execution_overrides_custom_workflow_action_completion() -> None:
    reason = _derive_zero_tool_execution_reason(
        selected_execution_mode="custom_workflow",
        tool_route_selected=True,
        tool_executed_count=0,
        failure_codes=["tool_dispatch_boundary_missing"],
        dispatch_terminal_status="completed",
        dispatch_terminal_completed=True,
        custom_workflow_execution={
            "action_completed_count": 2,
            "terminal_effect_count": 1,
        },
    )

    assert reason["zero_tool_reason_code"] == "tool_dispatch_boundary_missing"
    assert reason["zero_tool_execution_expected"] is False
    assert "no dispatch boundary evidence" in reason["zero_tool_reason"]


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
                            "failure_codes": ["required_tool_not_available_on_gateway"],
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
    progress_events: list[dict[str, Any]] = []
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
        "emit_progress": progress_events.append,
        "turn_id": "turn-completion-ready",
        "workflow_routing": {"selected_workflow_id": "#V#example_workflow"},
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
    ready_events = [
        event
        for event in progress_events
        if event.get("status") == "orchestrator_result_ready"
    ]
    assert ready_events
    assert ready_events[-1]["request_id"] == "turn-completion-ready"
    assert ready_events[-1]["response_text"] == "Grounded selected workflow answer."
    assert ready_events[-1]["workflow_routing"] == {
        "selected_workflow_id": "#V#example_workflow"
    }


def test_completion_gate_ready_progress_uses_completion_report_over_machine_json() -> (
    None
):
    progress_events: list[dict[str, Any]] = []
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
        "completion_report": {
            "response_text": "Here are the six recent email messages."
        },
        "selected_workflow_user_response": "",
        "final_response": "",
        "response_text": (
            '{"confidence": 1.0, "workflow_id": "#V#general_mail_review_workflow"}'
        ),
        "current_response": "",
        "invocations": [],
        "aux_llm_calls": [],
        "emit_progress": progress_events.append,
        "turn_id": "turn-completion-report-ready",
    }
    request = SimpleNamespace(
        data=data,
        inputs={},
        environment=SimpleNamespace(user_namespace="#V#user@org"),
    )

    run_turn_execution_completion_gate(
        request,
        annotation_component="test",
        annotation_function="test_completion_gate",
        introspection_auto_apply_env="VON_TEST_UNUSED",
    )

    ready_events = [
        event
        for event in progress_events
        if event.get("status") == "orchestrator_result_ready"
    ]
    assert ready_events
    assert ready_events[-1]["request_id"] == "turn-completion-report-ready"
    assert ready_events[-1]["response_text"] == (
        "Here are the six recent email messages."
    )


def test_completion_gate_ready_progress_renders_structured_response_text() -> None:
    progress_events: list[dict[str, Any]] = []
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
        "completion_report": {
            "response_text": [
                {
                    "sender": "Jacob Crandall <crandall@cs.byu.edu>",
                    "subject": "Re: Paper and chat?",
                    "date": "Thu, 18 Jun 2026 10:59:59 -0600",
                    "snippet": "Great. Let me know what times could work for you.",
                },
                {
                    "sender": "Admin <admin@example.test>",
                    "subject": "Weekly update",
                    "date": "Thu, 18 Jun 2026 09:42:00 -0600",
                    "snippet": "The weekly update is ready for review.",
                },
            ]
        },
        "selected_workflow_user_response": "",
        "final_response": "",
        "response_text": (
            '{"confidence": 1.0, "workflow_id": "#V#general_mail_review_workflow"}'
        ),
        "current_response": "",
        "invocations": [],
        "aux_llm_calls": [],
        "emit_progress": progress_events.append,
        "turn_id": "turn-structured-response-ready",
    }
    request = SimpleNamespace(
        data=data,
        inputs={},
        environment=SimpleNamespace(user_namespace="#V#user@org"),
    )

    run_turn_execution_completion_gate(
        request,
        annotation_component="test",
        annotation_function="test_completion_gate",
        introspection_auto_apply_env="VON_TEST_UNUSED",
    )

    ready_events = [
        event
        for event in progress_events
        if event.get("status") == "orchestrator_result_ready"
    ]
    assert ready_events
    rendered_text = ready_events[-1]["response_text"]
    assert ready_events[-1]["request_id"] == "turn-structured-response-ready"
    assert (
        "1. sender: Jacob Crandall <crandall@cs.byu.edu>; "
        "subject: Re: Paper and chat?; date: Thu, 18 Jun 2026 10:59:59 -0600; "
        "snippet: Great. Let me know what times could work for you."
    ) in rendered_text
    assert (
        "2. sender: Admin <admin@example.test>; subject: Weekly update; "
        "date: Thu, 18 Jun 2026 09:42:00 -0600; "
        "snippet: The weekly update is ready for review."
    ) in rendered_text


def test_completion_gate_does_not_publish_timeout_as_ready_response() -> None:
    progress_events: list[dict[str, Any]] = []
    data = {
        "turn_execution_record": {
            "completion_gate": {
                "decision": "needs_follow_up",
                "decision_reason": "Renderer timed out.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
            },
            "required_effects": [],
        },
        "completion_report": {
            "response_text": (
                "workflow_llm_step_timeout:LLM call timed out after 120s "
                "(stage=mail_review_response_rendering, model=qwen3:8b)"
            )
        },
        "selected_workflow_user_response": "",
        "final_response": "",
        "response_text": "",
        "current_response": "",
        "invocations": [],
        "aux_llm_calls": [],
        "emit_progress": progress_events.append,
        "turn_id": "turn-timeout-not-ready",
    }
    request = SimpleNamespace(
        data=data,
        inputs={},
        environment=SimpleNamespace(user_namespace="#V#user@org"),
    )

    run_turn_execution_completion_gate(
        request,
        annotation_component="test",
        annotation_function="test_completion_gate",
        introspection_auto_apply_env="VON_TEST_UNUSED",
    )

    assert [
        event
        for event in progress_events
        if event.get("status") == "orchestrator_result_ready"
    ] == []


def test_selected_workflow_outputs_render_structured_response_text() -> None:
    outputs = build_turn_execution_selected_workflow_outputs(
        selected_workflow_id="#V#general_mail_review_workflow",
        child_completed=True,
        final_state="complete",
        failure_detail=None,
        child_outputs={
            "completion_report": {
                "response_text": [
                    {
                        "sender": "Jacob Crandall <crandall@cs.byu.edu>",
                        "subject": "Re: Paper and chat?",
                        "date": "Thu, 18 Jun 2026 10:59:59 -0600",
                        "snippet": "Great. Let me know what times could work for you.",
                    }
                ]
            }
        },
    )

    assert outputs["selected_workflow_user_response"] == (
        "1. sender: Jacob Crandall <crandall@cs.byu.edu>; "
        "subject: Re: Paper and chat?; date: Thu, 18 Jun 2026 10:59:59 -0600; "
        "snippet: Great. Let me know what times could work for you."
    )
    assert outputs["completion_report"]["response_text"] == (
        outputs["selected_workflow_user_response"]
    )


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
    assert (
        projection["entries"][0]["projected_payload"]["messages"][0]["subject"]
        == "Lab scheduling"
    )
    assert "#V#gmail_subject_field" in projection["preserved_field_concept_ids"]


def test_completion_gate_blocks_operational_summary_when_authoritative_critic_flags_projected_evidence_mismatch() -> (
    None
):
    summary_only_answer = "Execution status: processed the Gmail request successfully."
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
                "failure_codes": ["projected_final_answer_evidence_not_consumed"],
                "repeat_eligible": True,
                "blocker_source": "critic_verdict",
            },
            "recommendations": [],
        },
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
    lineage = evidence_payload["requested_evidence_lineage"]
    assert lineage["final_response"]["text_checked_sha256"]
    assert lineage["final_response"]["text_checked_preview"] == summary_only_answer
    assert lineage["answer_consistency_blocker"]["failure_code"] == (
        "projected_final_answer_evidence_not_consumed"
    )
    assert lineage["requested_field_status_counts"]["satisfied"] == 4
    assert lineage["represented_contract_ids"] == [
        "#V#gmail_message_final_answer_evidence_view"
    ]
    assert lineage["completion_gate_safe_to_claim_completion"] is False


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


def test_target_type_required_evidence_rejects_other_target_success() -> None:
    record = build_turn_execution_record(
        request_id="req-target-type-required-evidence",
        session_id="session-target-type-required-evidence",
        namespace="#V#user@org",
        actor_concept_id="#V#user",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="What are key predicates for scientific papers in Vontology?",
        response_text="Predicate incidence was inspected.",
        interaction_timestamp_utc="2026-06-21T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        tool_invocations=[
            {
                "tool": "get_predicate_incidence",
                "status": "ok",
                "arguments": {"concept_id": "#V#michael_witbrock"},
                "payload": {
                    "success": True,
                    "concept_id": "#V#michael_witbrock",
                    "predicates": [],
                },
            }
        ],
        turn_expected_outcome_contract={
            "required_tools": ["get_predicate_incidence"],
            "target_type_ids": ["#V#scientific_paper"],
        },
    )

    gate = record["completion_gate"]
    assert gate["safe_to_claim_completion"] is False
    assert "prompt_required_evidence_get_predicate_incidence_wrong_target" in (
        gate["blocking_failure_codes"]
    )
    effect = next(
        item
        for item in record["required_effects"]
        if item["effect_id"] == (
            "effect_prompt_required_evidence_get_predicate_incidence_1"
        )
    )
    assert effect["targets"] == ["#V#scientific_paper"]
    assert effect["status"] == "not_executed"
    assert effect["failure_code"] == (
        "prompt_required_evidence_get_predicate_incidence_wrong_target"
    )


def test_target_type_required_evidence_accepts_instance_of_target() -> None:
    record = build_turn_execution_record(
        request_id="req-target-type-instance-of-evidence",
        session_id="session-target-type-instance-of-evidence",
        namespace="#V#user@org",
        actor_concept_id="#V#user",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="What are key predicates for a represented type?",
        response_text="Predicate incidence for the requested type was inspected.",
        interaction_timestamp_utc="2026-06-21T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        tool_invocations=[
            {
                "tool": "get_predicate_incidence",
                "status": "ok",
                "arguments": {"instance_of": "#V#requested_type"},
                "payload": {
                    "success": True,
                    "instance_of": "#V#requested_type",
                    "predicates": [],
                },
            }
        ],
        turn_expected_outcome_contract={
            "required_tools": ["get_predicate_incidence"],
            "target_type_ids": ["#V#requested_type"],
        },
    )

    gate = record["completion_gate"]
    assert gate["safe_to_claim_completion"] is True
    effect = next(
        item
        for item in record["required_effects"]
        if item["effect_id"] == (
            "effect_prompt_required_evidence_get_predicate_incidence_1"
        )
    )
    assert effect["status"] == "satisfied"


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
