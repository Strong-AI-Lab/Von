import json

from src.backend.services.turn_execution_record_service import (
    TURN_EXECUTION_CORRECTNESS_SCHEMA_VERSION,
    build_turn_execution_correctness_summary,
    build_turn_execution_record,
    build_workflow_routing_diagnostics,
    _summarise_tool_execution_context,
)
from src.backend.services.required_tool_obligation_service import (
    BLOCKER_CONTRACT_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY,
    BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED,
    BLOCKER_TOOL_BUDGET_EXHAUSTED_BEFORE_REQUIRED_TOOLS,
    build_required_tool_obligation_ledger,
)

_KR_REQUIRED_TOOLS = [
    "search_concepts",
    "create_concepts",
    "add_relationship",
    "upsert_singleton_text_relation",
    "fetch_concept",
    "get_text_relations_summary",
]


def test_turn_record_preserves_final_answer_synthesis_and_projection_telemetry() -> (
    None
):
    projected_tool_payload = {
        "tool": "gmail_list_messages",
        "status": "ok",
        "call_id": "tool-call-1",
        "payload": {
            "messages": [
                {"id": "msg-1", "subject": "Lab scheduling"},
                {"id": "msg-2", "subject": "Ontology review"},
            ],
            "_tool_evidence_projection": {
                "tool_concept_id": "#V#gmail_list_messages_tool",
                "evidence_view_concept_ids": [
                    "#V#gmail_message_final_answer_evidence_view"
                ],
                "preserved_fields": [
                    {
                        "field_concept_id": "#V#gmail_message_subject_field",
                        "output_key": "subject",
                        "location": "payload.messages[]",
                        "item_count": 2,
                    }
                ],
                "missing_required_fields": [
                    {
                        "field_concept_id": "#V#gmail_message_sender_field",
                        "output_key": "sender",
                        "reason": "missing_from_payload",
                    }
                ],
                "omitted_fields": [
                    {
                        "field_concept_id": "#V#gmail_message_snippet_field",
                        "output_key": "snippet",
                        "reason": "not_selected_for_view",
                    }
                ],
                "redacted_fields": [
                    {
                        "field_concept_id": "#V#gmail_message_body_field",
                        "output_key": "body",
                        "reason": "too_large_for_context",
                    }
                ],
            },
        },
    }
    aux_llm_calls = [
        {
            "type": "workflow_model_policy_stage",
            "stage": "summariser",
            "workflow_stage_id": "screen_backfill",
            "selected": {"provider": "openai", "model_resolved": "gpt-test"},
            "request": {
                "prompt": "Provide the final answer.",
                "context_messages": [
                    {"role": "system", "content": "system rules"},
                    {
                        "role": "tool",
                        "content": json.dumps(projected_tool_payload),
                    },
                ],
                "context_summary": {"message_count": 2},
                "context_lineage": {"added_message_count": 1},
                "tool_names": ["gmail_list_messages"],
                "tool_count": 1,
            },
        }
    ]
    llm_calls = [
        {
            "type": "llm.generate",
            "stage": "summariser",
            "workflow_stage_id": "screen_backfill",
            "provider": "openai",
            "model": "gpt-test",
            "duration_ms": 123,
            "exchange_blob_ref": {
                "collection": "llm_exchange_blobs",
                "blob_id": "blob-123",
                "sha256": "abc123",
            },
        }
    ]

    record = build_turn_execution_record(
        request_id="req-final-synthesis",
        session_id="session-final-synthesis",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Which messages did you find?",
        response_text="I found two relevant messages.",
        interaction_timestamp_utc="2026-04-20T01:00:00Z",
        workflow_routing={"verdict": "tool_calling", "source": "orchestrator"},
        tool_invocations=[],
        aux_llm_calls=aux_llm_calls,
        llm_calls=llm_calls,
    )

    synthesis = record["final_answer_synthesis"]
    assert synthesis["schema_version"] == "final_answer_synthesis_telemetry.v1"
    assert synthesis["exchange_blob_ref"]["blob_id"] == "blob-123"
    assert synthesis["request"]["prompt"]["text"] == "Provide the final answer."
    assert synthesis["context_lineage"] == {"added_message_count": 1}

    projection = synthesis["tool_evidence_projection"]
    assert projection["projection_count"] == 1
    assert projection["tools"] == ["gmail_list_messages"]
    assert projection["source_tool_invocation_ids"] == ["tool-call-1"]
    assert projection["entries"][0]["projected_payload"] == {
        "messages": [
            {"id": "msg-1", "subject": "Lab scheduling"},
            {"id": "msg-2", "subject": "Ontology review"},
        ]
    }
    assert projection["preserved_field_concept_ids"] == [
        "#V#gmail_message_subject_field"
    ]
    assert projection["missing_required_field_concept_ids"] == [
        "#V#gmail_message_sender_field"
    ]
    assert projection["omitted_field_concept_ids"] == ["#V#gmail_message_snippet_field"]
    assert projection["redacted_field_concept_ids"] == ["#V#gmail_message_body_field"]
    assert record["execution"]["summary"]["final_answer_synthesis_observed"] is True
    assert (
        record["execution"]["summary"]["final_answer_synthesis_projection_count"] == 1
    )
    assert record["final_response"]["synthesis_observed"] is True


def test_worker_unavailable_failure_code_only_applies_to_tool_routes() -> None:
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#chat_assistant_workflow",
            "verdict": "plain_response",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [
                    {
                        "liveness_reason": "worker_unavailable",
                        "event_kind": "heartbeat",
                        "stage": "orchestrator_start",
                        "phase": "orchestrator_start",
                    }
                ],
            }
        },
        aux_llm_calls=[],
        serialised_invocations=[],
    )

    assert summary["tool_route_selected"] is False
    assert "worker_unavailable_zero_execution" not in list(
        summary.get("failure_codes") or []
    )


def test_worker_unavailable_pre_dispatch_prepare_heartbeat_does_not_emit_tool_failure() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [
                    {
                        "liveness_reason": "worker_unavailable",
                        "event_kind": "heartbeat",
                        "stage": "workflow_dispatch_prepare",
                        "phase": "workflow_dispatch_prepare",
                    }
                ],
            }
        },
        aux_llm_calls=[],
        serialised_invocations=[],
    )

    assert summary["tool_route_selected"] is True
    assert summary["executed_count"] == 0
    assert "worker_unavailable_zero_execution" not in list(
        summary.get("failure_codes") or []
    )


def test_worker_unavailable_with_tool_plan_evidence_emits_tool_failure() -> None:
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [
                    {
                        "liveness_reason": "worker_unavailable",
                        "event_kind": "heartbeat",
                        "stage": "tool_plan",
                        "phase": "tool_plan",
                    }
                ],
            }
        },
        aux_llm_calls=[],
        serialised_invocations=[],
    )

    assert summary["tool_route_selected"] is True
    assert summary["executed_count"] == 0
    assert "worker_unavailable_zero_execution" in list(
        summary.get("failure_codes") or []
    )


def test_tool_execution_summary_uses_tool_call_end_events_for_executed_count() -> None:
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#chat_assistant_workflow",
            "verdict": "fallback",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [
                    {
                        "status": "tool_call_start",
                        "event_kind": "tool_call_start",
                        "phase": "tool_execute",
                        "tool": "turn_execution_list",
                        "call_id": "call-1",
                    },
                    {
                        "status": "tool_invoked",
                        "event_kind": "tool_call_end",
                        "phase": "tool_execute",
                        "tool": "turn_execution_list",
                        "call_id": "call-1",
                    },
                ],
            }
        },
        aux_llm_calls=[],
        serialised_invocations=[],
    )

    assert summary["tool_route_selected"] is False
    assert summary["started_count"] == 1
    assert summary["executed_count"] == 1


def test_tool_execution_summary_marks_missing_dispatch_boundary_after_tool_selection() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_selector",
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "rag_selected",
            }
        ],
        serialised_invocations=[],
    )

    assert summary["tool_route_selected"] is True
    assert summary["selected_execution_mode"] == "tool_pipeline"
    assert summary["last_successful_boundary"] == "workflow_selected"
    assert "tool_dispatch_boundary_missing" in list(summary.get("failure_codes") or [])


def test_tool_execution_summary_marks_missing_dispatch_boundary_after_custom_selection() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#arxiv_paper_representation_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_selector",
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "verdict": "rag_selected",
            }
        ],
        serialised_invocations=[],
    )

    assert summary["tool_route_selected"] is False
    assert summary["selected_execution_mode"] == "custom_workflow"
    assert summary["dispatch_workflow_id"] == "#V#arxiv_paper_representation_workflow"
    assert summary["last_successful_boundary"] == "workflow_selected"
    assert "custom_workflow_dispatch_not_started" in list(
        summary.get("failure_codes") or []
    )
    assert summary["custom_workflow_execution"]["observed"] is False


def test_custom_workflow_summary_uses_selected_workflow_trace_when_dispatch_events_missing() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#arxiv_paper_representation_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_selector",
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "verdict": "rag_selected",
            }
        ],
        serialised_invocations=[],
        selected_workflow_trace={
            "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
            "child_workflow_completed": False,
            "child_workflow_final_state": "failed",
            "child_workflow_error": "arxiv_mcp_server_missing_file_path",
            "completion_report_source": "child_completion_report",
        },
    )

    assert summary["dispatch_terminal_status"] == "failed"
    assert summary["dispatch_terminal_failure_reason"] == "child_workflow_failed"
    assert (
        summary["dispatch_terminal_failure_detail"]
        == "arxiv_mcp_server_missing_file_path"
    )
    assert "custom_workflow_dispatch_not_started" not in list(
        summary.get("failure_codes") or []
    )
    assert summary["last_successful_boundary"] == "workflow_terminal"
    assert summary["zero_tool_reason_code"] == (
        "custom_workflow_failed_before_tool_invocation"
    )
    assert summary["custom_workflow_execution"]["observed"] is True
    assert summary["custom_workflow_execution"]["final_state"] == "failed"
    assert (
        summary["custom_workflow_execution"]["completion_report_source"]
        == "child_completion_report"
    )


def test_custom_workflow_summary_uses_trace_execution_summary_when_aux_entry_missing() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#entity_information_retrieval_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_selector",
                "workflow_id": "#V#entity_information_retrieval_workflow",
                "verdict": "rag_selected",
            }
        ],
        serialised_invocations=[],
        selected_workflow_trace={
            "selected_workflow_id": "#V#entity_information_retrieval_workflow",
            "child_workflow_completed": True,
            "child_workflow_final_state": (
                "#V#workflow_step_entity_information_retrieval_workflow_completed"
            ),
            "completion_report_source": "child_completion_report",
            "workflow_execution_summary": {
                "schema_version": "workflow_execution_summary.v1",
                "workflow_id": "#V#entity_information_retrieval_workflow",
                "completed": True,
                "terminal_status": "completed",
                "final_state": (
                    "#V#workflow_step_entity_information_retrieval_workflow_completed"
                ),
                "step_result_envelope_count": 5,
                "action_started_count": 5,
                "action_completed_count": 5,
                "action_success_count": 4,
                "action_failure_count": 1,
                "action_unknown_count": 0,
                "runtime_event_count": 0,
                "terminal_effect_count": 1,
                "terminal_effects": [],
                "durable_side_effect_count": 0,
                "durable_side_effects": [],
            },
        },
    )

    custom_execution = summary["custom_workflow_execution"]
    assert custom_execution["observed"] is True
    assert custom_execution["workflow_id"] == "#V#entity_information_retrieval_workflow"
    assert custom_execution["action_started_count"] == 5
    assert custom_execution["action_completed_count"] == 5
    assert custom_execution["action_failure_count"] == 1
    assert custom_execution["completion_report_source"] == "child_completion_report"
    assert summary["zero_tool_reason_code"] == "custom_workflow_actions_handled_turn"
    assert summary["zero_tool_execution_expected"] is True


def test_tool_execution_summary_preserves_local_handoff_failure_reason() -> None:
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "execution_mode_selected",
                "status": "selected",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "contract_resolution",
                "status": "resolved",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_handoff",
                "status": "failed",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
                "reason": "tool_pipeline_setup_exception",
                "error_class": "RuntimeError",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_terminal",
                "status": "failed",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
                "reason": "tool_pipeline_setup_exception",
                "error_class": "RuntimeError",
                "completed": False,
            },
        ],
        serialised_invocations=[],
    )

    assert summary["workflow_handoff_started"] is False
    assert summary["workflow_handoff_failure_reason"] == "tool_pipeline_setup_exception"
    assert (
        summary["dispatch_terminal_failure_reason"] == "tool_pipeline_setup_exception"
    )
    assert summary["failure_codes"] == [
        "tool_pipeline_setup_exception",
        "tool_dispatch_not_started",
    ]
    assert summary["last_successful_boundary"] == "workflow_terminal"


def test_tool_execution_summary_preserves_custom_workflow_first_step_failure_locality() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#meeting_invitation_testing_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "execution_mode_selected",
                "status": "selected",
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#meeting_invitation_testing_workflow",
                "dispatch_workflow_id": "#V#meeting_invitation_testing_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_handoff",
                "status": "started",
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#meeting_invitation_testing_workflow",
                "dispatch_workflow_id": "#V#meeting_invitation_testing_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_terminal",
                "status": "failed",
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#meeting_invitation_testing_workflow",
                "dispatch_workflow_id": "#V#meeting_invitation_testing_workflow",
                "final_state": "prepare_spec",
                "completed": False,
                "reason": "workflow_launch_input_resolution_failed",
                "detail": (
                    "Workflow #V#meeting_invitation_testing_workflow could not start "
                    "because required launch inputs were unresolved: invitation_text."
                ),
                "workflow_launch_input_resolution_status": "failed",
                "unresolved_required_inputs": ["invitation_text"],
                "failing_state_id": "prepare_spec",
                "failing_action_id": "tool.prepare_spec",
            },
            {
                "type": "workflow_execution",
                "workflow_id": "#V#meeting_invitation_testing_workflow",
                "final_state": "prepare_spec",
                "completed": False,
                "execution_summary": {
                    "schema_version": "workflow_execution_summary.v1",
                    "workflow_id": "#V#meeting_invitation_testing_workflow",
                    "completed": False,
                    "final_state": "prepare_spec",
                    "step_result_envelope_count": 0,
                    "action_started_count": 0,
                    "action_completed_count": 0,
                    "action_success_count": 0,
                    "action_failure_count": 0,
                    "action_unknown_count": 0,
                    "first_failing_state_id": "prepare_spec",
                    "first_failing_action_id": "tool.prepare_spec",
                    "runtime_event_count": 0,
                    "terminal_effect_count": 0,
                    "terminal_effects": [],
                    "durable_side_effect_count": 0,
                    "durable_side_effects": [],
                },
            },
        ],
        serialised_invocations=[],
    )

    assert summary["tool_route_selected"] is False
    assert summary["selected_execution_mode"] == "custom_workflow"
    assert summary["dispatch_workflow_id"] == "#V#meeting_invitation_testing_workflow"
    assert summary["workflow_handoff_started"] is True
    assert summary["dispatch_terminal_status"] == "failed"
    assert summary["dispatch_terminal_final_state"] == "prepare_spec"
    assert summary["dispatch_terminal_failure_reason"] == (
        "workflow_launch_input_resolution_failed"
    )
    assert summary["dispatch_terminal_failure_detail"] == (
        "Workflow #V#meeting_invitation_testing_workflow could not start "
        "because required launch inputs were unresolved: invitation_text."
    )
    assert summary["dispatch_terminal_launch_input_resolution_status"] == "failed"
    assert summary["dispatch_terminal_unresolved_required_inputs"] == [
        "invitation_text"
    ]
    assert summary["dispatch_terminal_failing_state_id"] == "prepare_spec"
    assert summary["dispatch_terminal_failing_action_id"] == "tool.prepare_spec"
    custom_execution = summary["custom_workflow_execution"]
    assert custom_execution["observed"] is True
    assert custom_execution["schema_version"] == "workflow_execution_summary.v1"
    assert custom_execution["workflow_id"] == "#V#meeting_invitation_testing_workflow"
    assert custom_execution["completed"] is False
    assert custom_execution["final_state"] == "prepare_spec"
    assert custom_execution["step_result_envelope_count"] == 0
    assert custom_execution["action_started_count"] == 0
    assert custom_execution["action_completed_count"] == 0
    assert custom_execution["action_success_count"] == 0
    assert custom_execution["action_failure_count"] == 0
    assert custom_execution["action_unknown_count"] == 0
    assert custom_execution["first_failing_state_id"] == "prepare_spec"
    assert custom_execution["first_failing_action_id"] == "tool.prepare_spec"
    assert custom_execution["runtime_event_count"] == 0
    assert custom_execution["terminal_effect_count"] == 0
    assert custom_execution["terminal_effects"] == []
    assert custom_execution["durable_side_effect_count"] == 0
    assert custom_execution["durable_side_effects"] == []
    assert summary["zero_tools_executed"] is True
    assert summary["failure_codes"] == []
    assert summary["zero_tool_reason_code"] == (
        "custom_workflow_failed_before_tool_invocation"
    )
    assert summary["zero_tool_execution_expected"] is False


def test_tool_execution_summary_promotes_authoritative_submission_failure_to_dispatch_failure() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#conversation_turn_execution_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_instance_submission",
                "workflow_id": "#V#conversation_turn_execution_workflow",
                "status": "submission_failed",
                "reason_code": "workflow_not_runnable",
                "error": (
                    "Workflow #V#conversation_turn_execution_workflow is not runnable "
                    "because actions turn_execution.critic and "
                    "turn_execution.completion_gate are unsupported."
                ),
                "submission": {
                    "verification": {
                        "runnable_verification_success": False,
                        "unsupported_action_ids": [
                            "turn_execution.critic",
                            "turn_execution.completion_gate",
                        ],
                    }
                },
            }
        ],
        serialised_invocations=[],
    )

    assert summary["selected_execution_mode"] == "custom_workflow"
    assert summary["dispatch_workflow_id"] == "#V#conversation_turn_execution_workflow"
    assert summary["dispatch_terminal_status"] == "failed"
    assert summary["dispatch_terminal_failure_reason"] == "workflow_not_runnable"
    assert "turn_execution.critic" in (
        summary["dispatch_terminal_failure_detail"] or ""
    )


def test_tool_execution_summary_keeps_tool_pipeline_mode_for_submission_failure_without_boundaries() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_selector",
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "rag_selected",
            },
            {
                "type": "workflow_instance_submission",
                "workflow_id": "#V#tool_calling_workflow",
                "status": "submission_failed",
                "reason_code": "workflow_not_runnable",
                "error": (
                    "Workflow #V#tool_calling_workflow is not runnable because the "
                    "tool pipeline boundary was missing."
                ),
                "submission": {
                    "verification": {
                        "runnable_verification_success": False,
                    }
                },
            },
        ],
        serialised_invocations=[],
    )

    assert summary["selected_execution_mode"] == "tool_pipeline"
    assert summary["dispatch_workflow_id"] == "#V#tool_calling_workflow"
    assert summary["dispatch_terminal_status"] == "failed"
    assert summary["dispatch_terminal_failure_reason"] == "workflow_not_runnable"
    assert "tool_dispatch_boundary_missing" in list(summary.get("failure_codes") or [])
    assert "custom_workflow_dispatch_not_started" not in list(
        summary.get("failure_codes") or []
    )


def test_tool_execution_summary_records_custom_workflow_action_and_side_effect_evidence() -> (
    None
):
    summary = _summarise_tool_execution_context(
        workflow_routing={
            "workflow_id": "#V#workflow_creation_workflow",
            "verdict": "rag_selected",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "execution_mode_selected",
                "status": "selected",
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#workflow_creation_workflow",
                "dispatch_workflow_id": "#V#workflow_creation_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_handoff",
                "status": "started",
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#workflow_creation_workflow",
                "dispatch_workflow_id": "#V#workflow_creation_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_terminal",
                "status": "completed",
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#workflow_creation_workflow",
                "dispatch_workflow_id": "#V#workflow_creation_workflow",
                "final_state": "done",
                "completed": True,
            },
            {
                "type": "workflow_execution",
                "workflow_id": "#V#workflow_creation_workflow",
                "final_state": "done",
                "completed": True,
                "execution_summary": {
                    "schema_version": "workflow_execution_summary.v1",
                    "workflow_id": "#V#workflow_creation_workflow",
                    "completed": True,
                    "final_state": "done",
                    "step_result_envelope_count": 2,
                    "action_started_count": 2,
                    "action_completed_count": 2,
                    "action_success_count": 2,
                    "action_failure_count": 0,
                    "action_unknown_count": 0,
                    "runtime_event_count": 4,
                    "terminal_effect_count": 1,
                    "terminal_effects": [
                        {
                            "state_id": "done",
                            "symbol": "#V#workflow_effect_workflow_creation_done_terminal",
                            "alias": "workflow_effect_workflow_creation_done_terminal",
                            "applied": True,
                        }
                    ],
                    "durable_side_effect_count": 2,
                    "durable_side_effects": [
                        {
                            "mutation_kind": "created",
                            "artefact_type": "workflow",
                            "source_key": "created_workflow_ids",
                            "source_path": "created_workflow_ids",
                            "artefact_count": 1,
                            "artefact_ids": ["#V#wf_new"],
                        },
                        {
                            "mutation_kind": "updated",
                            "artefact_type": "type",
                            "source_key": "updated_type_ids",
                            "source_path": "alignment.updated_type_ids",
                            "artefact_count": 1,
                            "artefact_ids": ["#V#durable_workflow"],
                        },
                    ],
                },
            },
        ],
        serialised_invocations=[],
    )

    custom_execution = summary["custom_workflow_execution"]
    assert custom_execution["observed"] is True
    assert custom_execution["workflow_id"] == "#V#workflow_creation_workflow"
    assert custom_execution["action_completed_count"] == 2
    assert custom_execution["action_success_count"] == 2
    assert custom_execution["terminal_effect_count"] == 1
    assert custom_execution["durable_side_effect_count"] == 2
    assert custom_execution["durable_side_effects"] == [
        {
            "mutation_kind": "created",
            "artefact_type": "workflow",
            "source_key": "created_workflow_ids",
            "source_path": "created_workflow_ids",
            "artefact_count": 1,
            "artefact_ids": ["#V#wf_new"],
        },
        {
            "mutation_kind": "updated",
            "artefact_type": "type",
            "source_key": "updated_type_ids",
            "source_path": "alignment.updated_type_ids",
            "artefact_count": 1,
            "artefact_ids": ["#V#durable_workflow"],
        },
    ]
    assert summary["zero_tools_executed"] is True
    assert summary["zero_tool_reason_code"] == "custom_workflow_actions_handled_turn"
    assert summary["zero_tool_execution_expected"] is True


def test_turn_execution_record_keeps_custom_workflow_execution_consistent_across_surfaces() -> (
    None
):
    aux_llm_calls = [
        {
            "type": "workflow_dispatch_boundary",
            "boundary": "execution_mode_selected",
            "status": "selected",
            "selected_execution_mode": "custom_workflow",
            "selected_workflow_id": "#V#workflow_creation_workflow",
            "dispatch_workflow_id": "#V#workflow_creation_workflow",
        },
        {
            "type": "workflow_dispatch_boundary",
            "boundary": "workflow_handoff",
            "status": "started",
            "selected_execution_mode": "custom_workflow",
            "selected_workflow_id": "#V#workflow_creation_workflow",
            "dispatch_workflow_id": "#V#workflow_creation_workflow",
        },
        {
            "type": "workflow_dispatch_boundary",
            "boundary": "workflow_terminal",
            "status": "completed",
            "selected_execution_mode": "custom_workflow",
            "selected_workflow_id": "#V#workflow_creation_workflow",
            "dispatch_workflow_id": "#V#workflow_creation_workflow",
            "final_state": "done",
            "completed": True,
        },
        {
            "type": "workflow_execution",
            "workflow_id": "#V#workflow_creation_workflow",
            "final_state": "done",
            "completed": True,
            "execution_summary": {
                "schema_version": "workflow_execution_summary.v1",
                "workflow_id": "#V#workflow_creation_workflow",
                "completed": True,
                "final_state": "done",
                "step_result_envelope_count": 1,
                "action_started_count": 1,
                "action_completed_count": 1,
                "action_success_count": 1,
                "action_failure_count": 0,
                "action_unknown_count": 0,
                "runtime_event_count": 2,
                "terminal_effect_count": 1,
                "terminal_effects": [
                    {
                        "state_id": "done",
                        "symbol": "#V#workflow_effect_workflow_creation_done_terminal",
                        "alias": "workflow_effect_workflow_creation_done_terminal",
                        "applied": True,
                    }
                ],
                "durable_side_effect_count": 1,
                "durable_side_effects": [
                    {
                        "mutation_kind": "created",
                        "artefact_type": "workflow",
                        "source_key": "created_workflow_ids",
                        "source_path": "created_workflow_ids",
                        "artefact_count": 1,
                        "artefact_ids": ["#V#wf_new"],
                    }
                ],
            },
        },
    ]
    record = build_turn_execution_record(
        request_id="req-custom-1",
        session_id="session-custom-1",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Create the workflow definition.",
        response_text="Workflow created.",
        interaction_timestamp_utc="2026-03-24T01:00:00Z",
        workflow_discovery={
            "matches": [{"concept_id": "#V#workflow_creation_workflow"}]
        },
        workflow_routing={
            "workflow_id": "#V#workflow_creation_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        tool_invocations=[],
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=aux_llm_calls,
    )

    summary = record["execution"]["summary"]
    dispatch = record["workflow_routing_diagnostics"]["dispatch"]
    summary_custom_execution = dict(summary["custom_workflow_execution"])
    dispatch_custom_execution = dict(dispatch["custom_workflow_execution"])
    assert {
        key: summary_custom_execution.get(key)
        for key in dispatch_custom_execution.keys()
    } == dispatch_custom_execution
    assert summary_custom_execution["completion_report_source"] is None
    assert summary_custom_execution["error"] is None
    assert summary_custom_execution["result_snapshot"] is None
    assert summary["zero_tool_reason_code"] == "custom_workflow_actions_handled_turn"
    assert dispatch["zero_tool_reason_code"] == "custom_workflow_actions_handled_turn"
    assert dispatch["custom_workflow_execution"]["durable_side_effects"] == [
        {
            "mutation_kind": "created",
            "artefact_type": "workflow",
            "source_key": "created_workflow_ids",
            "source_path": "created_workflow_ids",
            "artefact_count": 1,
            "artefact_ids": ["#V#wf_new"],
        }
    ]


def test_turn_record_uses_selected_workflow_trace_for_supervised_custom_failure() -> (
    None
):
    record = build_turn_execution_record(
        request_id="req-selected-trace-failure",
        session_id="session-selected-trace-failure",
        namespace="#V#user",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="https://arxiv.org/abs/2603.18678",
        response_text=(
            "I couldn't complete that request because the authoritative "
            "conversation-turn workflow failed."
        ),
        interaction_timestamp_utc="2026-04-12T02:24:06Z",
        workflow_routing={
            "workflow_id": "#V#arxiv_paper_representation_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        tool_invocations=[],
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_selector",
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "verdict": "rag_selected",
            }
        ],
        selected_workflow_trace={
            "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
            "child_workflow_completed": False,
            "child_workflow_final_state": "failed",
            "child_workflow_error": "arxiv_mcp_server_missing_file_path",
            "completion_report_source": "child_completion_report",
        },
        completion_report={
            "response_text": (
                "arXiv download succeeded but no file path was returned by "
                "arxiv-mcp-server"
            )
        },
    )

    summary = record["execution"]["summary"]
    assert summary["dispatch_terminal_status"] == "failed"
    assert summary["dispatch_terminal_failure_reason"] == "child_workflow_failed"
    assert (
        summary["dispatch_terminal_failure_detail"]
        == "arxiv_mcp_server_missing_file_path"
    )
    assert summary["custom_workflow_execution"]["observed"] is True

    workflow_effects = [
        effect
        for effect in record["required_effects"]
        if effect.get("effect_type") == "workflow_execution"
    ]
    assert workflow_effects
    assert workflow_effects[0]["status"] == "not_satisfied"
    assert record["completion_gate"]["decision"] in {"failed", "partial"}
    assert (
        record["execution"]["selected_workflow_trace"]["child_workflow_error"]
        == "arxiv_mcp_server_missing_file_path"
    )


def test_turn_record_preserves_first_class_turn_expected_outcome_contract_snapshot() -> (
    None
):
    expected_contract = {
        "summary": "Answer only with grounded represented records.",
        "grounding_requirement": (
            "Only surface records that are grounded in represented evidence."
        ),
        "precision_policy": "Prefer omission over unsupported claims.",
    }
    record = build_turn_execution_record(
        request_id="req-expected-contract-1",
        session_id="session-expected-contract-1",
        namespace="#V#user",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="List grounded represented records linked to the current user.",
        response_text="Example Record",
        interaction_timestamp_utc="2026-04-20T06:12:00Z",
        workflow_discovery={
            "matches": [{"concept_id": "#V#tool_calling_workflow"}],
            "turn_expected_outcome_contract": expected_contract,
        },
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        selected_workflow_trace={
            "selected_workflow_id": "#V#tool_calling_workflow",
            "expected_outcome_contract": expected_contract,
        },
        turn_expected_outcome_contract={
            "schema_version": "turn_expected_outcome_contract.v1",
            "fields": expected_contract,
            "field_count": len(expected_contract),
            "sources": ["turn_expected_outcome_contract"],
        },
        tool_invocations=[],
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[],
    )

    assert record["turn_expected_outcome_contract"] == expected_contract
    contract_state = record["turn_expected_outcome_contract_state"]
    assert contract_state["schema_version"] == "turn_expected_outcome_contract.v1"
    assert contract_state["fields"] == expected_contract
    assert (
        record["workflow_routing_diagnostics"]["turn_expected_outcome_contract"]
        == expected_contract
    )
    assert (
        record["execution"]["selected_workflow_trace"]["expected_outcome_contract"]
        == expected_contract
    )
    assert (
        record["execution"]["selected_workflow_trace"][
            "expected_outcome_contract_state"
        ]["fields"]
        == expected_contract
    )
    assert record["execution"]["summary"][
        "turn_expected_outcome_contract_field_count"
    ] == len(expected_contract)


def test_build_workflow_routing_diagnostics_surfaces_discovery_stage_timings() -> None:
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery={
            "query": "who am i in this conversation",
            "candidate_count": 0,
            "match_count": 0,
            "candidates": [],
            "stage_timings": [
                {
                    "stage": "capability_index_search",
                    "status": "ok",
                    "elapsed_ms": 12000.5,
                },
                {"stage": "semantic_search", "status": "ok", "elapsed_ms": 45123.7},
                {"stage": "vontology_search", "status": "ok", "elapsed_ms": 800.2},
                {"stage": "enrich_matches", "status": "ok", "elapsed_ms": 50.0},
            ],
        },
        workflow_routing={},
        turn_execution_diagnostics={},
        aux_llm_calls=[],
    )
    discovery_block = diagnostics["discovery"]
    assert discovery_block["stage_timing_count"] == 4
    stage_timings = discovery_block["stage_timings"]
    assert [entry["stage"] for entry in stage_timings] == [
        "capability_index_search",
        "semantic_search",
        "vontology_search",
        "enrich_matches",
    ]
    slowest = discovery_block["slowest_stages"]
    assert [entry["stage"] for entry in slowest[:2]] == [
        "semantic_search",
        "capability_index_search",
    ]
    assert slowest[0]["elapsed_ms"] == 45123.7


def test_build_workflow_routing_diagnostics_surfaces_discovery_payload_origin() -> None:
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery={
            "query": "who am i in this conversation",
            "discovery_payload_origin": "discover_workflows_for_turn",
            "candidates": [],
            "candidate_count": 0,
            "match_count": 0,
        },
        workflow_routing={},
        turn_execution_diagnostics={},
        aux_llm_calls=[],
    )
    discovery_block = diagnostics["discovery"]
    assert discovery_block["discovery_payload_origin"] == "discover_workflows_for_turn"


def test_build_workflow_routing_diagnostics_marks_missing_discovery_payload() -> None:
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery=None,
        workflow_routing={},
        turn_execution_diagnostics={},
        aux_llm_calls=[],
    )
    assert (
        diagnostics["discovery"]["discovery_payload_origin"]
        == "missing_discovery_payload"
    )


def test_build_workflow_routing_diagnostics_marks_unstamped_discovery_payload() -> None:
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery={
            "query": "who am i",
            "candidates": [],
            "candidate_count": 0,
            "match_count": 0,
        },
        workflow_routing={},
        turn_execution_diagnostics={},
        aux_llm_calls=[],
    )
    assert (
        diagnostics["discovery"]["discovery_payload_origin"]
        == "unstamped_discovery_payload"
    )


def test_build_workflow_routing_diagnostics_preserves_selector_exchange_and_dispatch_events() -> (
    None
):
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery={
            "query": "meeting invitation testing workflow",
            "candidate_count": 2,
            "match_count": 0,
            "candidates": [
                {
                    "concept_id": "#V#meeting_invitation_testing_workflow",
                    "name": "Meeting invitation testing workflow",
                    "description": "Materialise a meeting invitation test run.",
                    "routing_eligible": False,
                    "routing_exclusion_reason": "missing_authoritative_purpose",
                },
                {
                    "concept_id": "#V#tool_calling_workflow",
                    "name": "Tool calling workflow",
                    "description": "General-purpose tool workflow",
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
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_dispatch_prepare_step",
                "step_id": "workflow_model_policy",
                "step_label": "Load routing model policy",
                "status": "completed",
                "duration_ms": 7,
            },
            {
                "type": "workflow_dispatch_prepare_step",
                "step_id": "selector_candidate_preparation",
                "step_label": "Prepare selector candidates",
                "status": "completed",
                "duration_ms": 11,
            },
            {
                "type": "workflow_dispatch_turn_contract_check",
                "status": "override_required",
                "selected_workflow_id": "#V#meeting_invitation_testing_workflow",
                "selected_workflow_can_satisfy_contract": False,
                "required_tools": [
                    "search_knowledge_base",
                    "search_concepts",
                    "search_web",
                ],
                "required_surface_families": [
                    "knowledge_base",
                    "web",
                ],
                "external_surface_families": ["web"],
                "override_reason": (
                    "selected_custom_workflow_cannot_satisfy_multi_surface_turn_contract"
                ),
                "reasoning": (
                    "The selected custom workflow did not advertise tool-pipeline "
                    "execution, so dispatch had to use the general tool workflow."
                ),
                "turn_expected_outcome_contract": {
                    "success_target": "Grounded meeting invitation test plan.",
                    "selector_guidance": (
                        "Use represented meeting context and live web confirmation."
                    ),
                },
            },
            {
                "type": "workflow_selector_prompt",
                "prompt_id": "#V#chat_turn_classifier_prompt",
                "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
                "prompt_provenance": {
                    "prompt_mode": "rag_first_candidate_selector",
                    "resolved_prompt_id": "#V#chat_turn_classifier_prompt",
                    "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
                    "render_variables": {
                        "turn_text": "Run the meeting invitation test",
                        "candidate_list": "- #V#meeting_invitation_testing_workflow",
                    },
                    "truncated": False,
                },
                "candidate_list": {
                    "text": "- #V#meeting_invitation_testing_workflow",
                    "char_count": 40,
                },
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
                                "text": "CURRENT USER CONTEXT: Test User (#V#test_user)",
                                "char_count": 46,
                            },
                        },
                        {
                            "role": "system",
                            "content": {
                                "text": "Selector system prompt",
                                "char_count": 22,
                            },
                        },
                        {
                            "role": "user",
                            "content": {
                                "text": "Run the meeting invitation test",
                                "char_count": 31,
                            },
                        },
                    ],
                    "context_message_count": 3,
                    "context_summary": {
                        "message_count": 3,
                        "leading_system_message_count": 2,
                        "role_counts": {"system": 2, "user": 1},
                        "total_content_chars": 99,
                    },
                    "context_lineage": {
                        "stage": "workflow_dispatch",
                        "base_context_source": "augmented_context",
                        "insertion_strategy": "after_leading_system",
                        "base_context_summary": {
                            "message_count": 2,
                            "leading_system_message_count": 1,
                            "role_counts": {"system": 1, "user": 1},
                            "total_content_chars": 77,
                        },
                        "stage_added_message_count": 1,
                        "stage_added_messages": [
                            {
                                "role": "system",
                                "content_preview": "Selector system prompt",
                                "content_char_count": 22,
                            }
                        ],
                        "stage_context_summary": {
                            "message_count": 3,
                            "leading_system_message_count": 2,
                            "role_counts": {"system": 2, "user": 1},
                            "total_content_chars": 99,
                        },
                    },
                },
                "selected": {
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "model_resolved": "gpt-5-mini",
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
                        "error": "connection refused",
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
                "errors": [
                    {
                        "model_resolved": "granite3.3:2b",
                        "failure_kind": "provider_unreachable",
                        "error": "connection refused",
                    }
                ],
            },
            {
                "type": "workflow_selector",
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "rag_selected",
                "model_name": "gpt-5-mini",
                "prompt": {"text": "Select workflow", "char_count": 15},
                "prompt_provenance": {
                    "prompt_mode": "rag_first_candidate_selector",
                    "resolved_prompt_id": "#V#chat_turn_classifier_prompt",
                    "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
                    "render_variables": {
                        "turn_text": "Run the meeting invitation test",
                        "candidate_list": "- #V#meeting_invitation_testing_workflow",
                    },
                },
                "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
                "candidate_list": {
                    "text": "- #V#meeting_invitation_testing_workflow",
                    "char_count": 40,
                },
                "context_lineage": {
                    "stage": "workflow_dispatch",
                    "base_context_source": "augmented_context",
                    "stage_added_message_count": 1,
                },
                "response": {
                    "text": "#V#tool_calling_workflow",
                    "char_count": 24,
                },
                "candidate_entries": [
                    {
                        "concept_id": "#V#meeting_invitation_testing_workflow",
                        "name": "Meeting invitation testing workflow",
                        "description": "Materialise a meeting invitation test run.",
                        "candidate_source": "workflow_discovery",
                        "candidate_reason": "discovered_workflow_candidate",
                    },
                    {
                        "concept_id": "#V#tool_calling_workflow",
                        "name": "Tool calling workflow",
                        "description": "General-purpose tool workflow",
                        "candidate_source": "selector_default",
                        "candidate_reason": "builtin_selector_candidate",
                    },
                ],
                "discovery_excluded_candidates": [
                    {
                        "concept_id": "#V#meeting_invitation_testing_workflow",
                        "routing_eligible": False,
                        "routing_exclusion_reason": "missing_authoritative_purpose",
                    }
                ],
                "selection_metadata": {
                    "selection_resolution": "candidate_label_exact_match",
                    "raw_candidate_label": "#V#tool_calling_workflow",
                    "raw_response_format": "text",
                },
                "selection_rationale": "selector_selected_discovered_candidate",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "execution_mode_selected",
                "status": "selected",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "contract_resolution",
                "status": "resolved",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
            },
        ],
        execution_summary={
            "tool_route_selected": True,
            "selected_execution_mode": "tool_pipeline",
            "dispatch_workflow_id": "#V#tool_calling_workflow",
            "dispatch_event_count": 2,
            "contract_resolution_status": "resolved",
            "workflow_handoff_started": False,
            "dispatch_terminal_status": None,
            "dispatch_terminal_final_state": None,
            "dispatch_terminal_completed": None,
            "planned_count": 1,
            "started_count": 0,
            "executed_count": 0,
            "zero_tools_executed": True,
            "failure_codes": ["tool_dispatch_not_started"],
            "last_successful_boundary": "contract_resolution",
        },
    )

    assert diagnostics["schema_version"] == "workflow_routing_diagnostics.v1"
    assert diagnostics["selector"]["prompt"]["text"] == "Select workflow"
    assert diagnostics["selector"]["response"]["text"] == "#V#tool_calling_workflow"
    assert diagnostics["selector"]["model_name"] == "gpt-5-mini"
    assert diagnostics["selector"]["prompt_provenance"]["resolved_prompt_id"] == (
        "#V#chat_turn_classifier_prompt"
    )
    assert diagnostics["selector"]["candidate_list"]["text"] == (
        "- #V#meeting_invitation_testing_workflow"
    )
    assert diagnostics["selector"]["requested_prompt_ids"] == [
        "#V#chat_turn_classifier_prompt"
    ]
    assert diagnostics["selector"]["candidate_source_counts"] == [
        {"name": "selector_default", "count": 1},
        {"name": "workflow_discovery", "count": 1},
    ]
    assert diagnostics["selector"]["model_request"]["prompt"]["text"] == (
        "Select workflow"
    )
    assert diagnostics["selector"]["model_request"]["context_messages"][0]["role"] == (
        "system"
    )
    assert diagnostics["selector"]["model_request"]["context_summary"] == {
        "message_count": 3,
        "leading_system_message_count": 2,
        "role_counts": {"system": 2, "user": 1},
        "total_content_chars": 99,
    }
    assert diagnostics["selector"]["context_lineage"]["base_context_source"] == (
        "augmented_context"
    )
    assert diagnostics["selector"]["context_lineage"]["stage_added_message_count"] == 1
    assert diagnostics["selector"]["model_attempts"][0]["failure_kind"] == (
        "provider_unreachable"
    )
    assert diagnostics["selector"]["primary_fallback_failure_kind"] == (
        "provider_unreachable"
    )
    assert diagnostics["selector"]["fallback_failure_kind_counts"] == [
        {"name": "provider_unreachable", "count": 1}
    ]
    assert diagnostics["selector"]["model_attempts"][1]["response"]["text"] == (
        "#V#tool_calling_workflow"
    )
    assert diagnostics["selector"]["selection_resolution"] == (
        "candidate_label_exact_match"
    )
    assert diagnostics["selector"]["candidate_entries"][0]["concept_id"] == (
        "#V#meeting_invitation_testing_workflow"
    )
    assert diagnostics["discovery"]["excluded_candidates"][0]["concept_id"] == (
        "#V#meeting_invitation_testing_workflow"
    )
    assert diagnostics["discovery"]["match_absence_reason"] == (
        "no_routing_match_after_exclusions"
    )
    assert diagnostics["dispatch"]["pre_dispatch"]["step_count"] == 2
    assert diagnostics["dispatch"]["pre_dispatch"]["total_duration_ms"] == 18
    assert diagnostics["dispatch"]["pre_dispatch"]["slowest_step_id"] == (
        "selector_candidate_preparation"
    )
    assert diagnostics["dispatch"]["turn_contract_check"]["status"] == (
        "override_required"
    )
    assert diagnostics["dispatch"]["turn_contract_check"]["selected_workflow_id"] == (
        "#V#meeting_invitation_testing_workflow"
    )
    assert (
        diagnostics["dispatch"]["turn_contract_check"][
            "selected_workflow_can_satisfy_contract"
        ]
        is False
    )
    assert diagnostics["dispatch"]["turn_contract_check"]["required_tools"] == [
        "search_knowledge_base",
        "search_concepts",
        "search_web",
    ]
    assert diagnostics["dispatch"]["turn_contract_check"][
        "required_surface_families"
    ] == [
        "knowledge_base",
        "web",
    ]
    assert diagnostics["dispatch"]["turn_contract_check"][
        "external_surface_families"
    ] == ["web"]
    assert diagnostics["dispatch"]["turn_contract_check"]["override_reason"] == (
        "selected_custom_workflow_cannot_satisfy_multi_surface_turn_contract"
    )
    assert diagnostics["dispatch"]["turn_contract_check"][
        "turn_expected_outcome_contract"
    ] == {
        "success_target": "Grounded meeting invitation test plan.",
        "selector_guidance": (
            "Use represented meeting context and live web confirmation."
        ),
    }
    assert diagnostics["dispatch"]["selected_execution_mode"] == "tool_pipeline"
    assert diagnostics["dispatch"]["contract_resolution_status"] == "resolved"
    assert diagnostics["dispatch"]["failure_codes"] == ["tool_dispatch_not_started"]
    assert diagnostics["dispatch"]["zero_execution_primary_failure_code"] == (
        "tool_dispatch_not_started"
    )
    assert diagnostics["dispatch"]["last_successful_boundary"] == "contract_resolution"


def test_build_workflow_routing_diagnostics_preserves_local_handoff_failure_details() -> (
    None
):
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery={
            "query": "Run the testing workflow",
            "matches": [],
            "candidates": [],
        },
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "execution_mode_selected",
                "status": "selected",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "contract_resolution",
                "status": "resolved",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_handoff",
                "status": "failed",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
                "reason": "tool_pipeline_setup_exception",
                "error_class": "RuntimeError",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_terminal",
                "status": "failed",
                "selected_execution_mode": "tool_pipeline",
                "selected_workflow_id": "#V#tool_calling_workflow",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
                "reason": "tool_pipeline_setup_exception",
                "error_class": "RuntimeError",
                "completed": False,
            },
        ],
        execution_summary={
            "tool_route_selected": True,
            "selected_execution_mode": "tool_pipeline",
            "dispatch_workflow_id": "#V#tool_calling_workflow",
            "dispatch_event_count": 4,
            "contract_resolution_status": "resolved",
            "workflow_handoff_started": False,
            "workflow_handoff_failure_reason": "tool_pipeline_setup_exception",
            "workflow_handoff_failure_error_class": "RuntimeError",
            "dispatch_terminal_status": "failed",
            "dispatch_terminal_final_state": None,
            "dispatch_terminal_completed": False,
            "dispatch_terminal_failure_reason": "tool_pipeline_setup_exception",
            "dispatch_terminal_failure_error_class": "RuntimeError",
            "planned_count": 1,
            "started_count": 0,
            "executed_count": 0,
            "zero_tools_executed": True,
            "failure_codes": [
                "tool_pipeline_setup_exception",
                "tool_dispatch_not_started",
            ],
            "last_successful_boundary": "workflow_terminal",
        },
    )

    assert diagnostics["dispatch"]["workflow_handoff_failure_reason"] == (
        "tool_pipeline_setup_exception"
    )
    assert diagnostics["dispatch"]["workflow_handoff_failure_error_class"] == (
        "RuntimeError"
    )
    assert diagnostics["dispatch"]["dispatch_terminal_failure_reason"] == (
        "tool_pipeline_setup_exception"
    )
    assert diagnostics["dispatch"]["zero_execution_primary_failure_code"] == (
        "tool_pipeline_setup_exception"
    )


def test_build_workflow_routing_diagnostics_derives_capability_index_timeout_cause() -> (
    None
):
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery={
            "query": "Download and represent https://arxiv.org/abs/2411.04983",
            "matches": [],
            "candidates": [],
            "errors": [
                "capability_index_wait_timed_out",
                "capability_index_build_in_progress",
            ],
        },
        workflow_routing={},
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {"tools_started": 0, "tools_completed": 0},
                "diagnostic_events": [],
            }
        },
        aux_llm_calls=[],
        execution_summary={},
    )

    assert diagnostics["discovery"]["match_absence_reason"] == (
        "capability_index_wait_timed_out_build_in_progress"
    )
    assert diagnostics["discovery"]["errors"] == [
        "capability_index_wait_timed_out",
        "capability_index_build_in_progress",
    ]


def test_build_turn_execution_correctness_summary_marks_successful_completion() -> None:
    summary = build_turn_execution_correctness_summary(
        completion_gate={
            "decision": "completed",
            "decision_reason": "No blocking effect detected.",
            "safe_to_claim_completion": True,
            "requires_follow_up": False,
        },
        required_effects=[],
        critic_summary={"not_verified_count": 0, "inconclusive_count": 0},
        final_response={
            "completion_claim_detected": False,
            "completion_claim_validated": True,
        },
        workflow_selection={
            "selected_workflow_id": "#V#chat_assistant_workflow",
            "selector_verdict": "plain_response",
            "selector_source": "default",
        },
        workflow_routing_diagnostics={},
    )

    assert summary["schema_version"] == TURN_EXECUTION_CORRECTNESS_SCHEMA_VERSION
    assert summary["overall_outcome"] == "successful_completion"
    assert summary["failure_mode"] == "completed_verified"
    assert summary["likely_failure_to_act"] is False
    assert summary["metric_labels"]["successful_completion"] is True
    assert summary["metric_labels"]["false_success"] is False


def test_build_turn_execution_correctness_summary_marks_plain_response_misrouting() -> (
    None
):
    summary = build_turn_execution_correctness_summary(
        completion_gate={
            "decision": "escalation_required",
            "decision_reason": "Required mutation was not executed.",
            "safe_to_claim_completion": False,
            "requires_follow_up": True,
        },
        required_effects=[{"effect_id": "effect_1", "status": "not_executed"}],
        critic_summary={"not_verified_count": 1},
        final_response={
            "completion_claim_detected": True,
            "completion_claim_validated": False,
        },
        workflow_selection={
            "selected_workflow_id": "#V#chat_assistant_workflow",
            "selector_verdict": "plain_response",
            "selector_source": "default",
        },
        workflow_routing_diagnostics={},
    )

    assert summary["schema_version"] == TURN_EXECUTION_CORRECTNESS_SCHEMA_VERSION
    assert summary["failure_mode"] == "mutation_not_executed"
    assert summary["overall_outcome"] == "tool_or_workflow_misrouting"
    assert summary["likely_failure_to_act"] is True
    assert summary["metric_labels"]["unresolved_follow_up_needed"] is True
    assert summary["metric_labels"]["tool_or_workflow_misrouting"] is True
    assert summary["selection_labels"]["plain_response_route_selected"] is True


def test_build_turn_execution_correctness_summary_marks_launchability_fallback_misrouting() -> (
    None
):
    summary = build_turn_execution_correctness_summary(
        completion_gate={
            "decision": "escalation_required",
            "decision_reason": "Selected workflow was not executed.",
            "safe_to_claim_completion": False,
            "requires_follow_up": True,
        },
        required_effects=[{"effect_id": "effect_1", "status": "not_executed"}],
        critic_summary={"not_verified_count": 1},
        final_response={
            "completion_claim_detected": True,
            "completion_claim_validated": False,
        },
        workflow_selection={
            "selected_workflow_id": "#V#tool_calling_workflow",
            "selector_verdict": "tool_contract_override",
            "selector_source": "selector_override",
        },
        workflow_routing_diagnostics={
            "selector": {
                "override_events": [
                    {
                        "reason": "selected_custom_workflow_launchability_requires_safe_general_fallback",
                        "prior_selected_workflow_id": "#V#arxiv_paper_representation_workflow",
                        "selected_workflow_id": "#V#tool_calling_workflow",
                        "custom_workflow_override_reason": "no_custom_workflow_candidates",
                        "launch_viability_probe": {
                            "prior_selected_workflow": {"launchable": False}
                        },
                    }
                ]
            }
        },
    )

    assert summary["failure_mode"] == "mutation_not_executed"
    assert summary["overall_outcome"] == "tool_or_workflow_misrouting"
    assert summary["likely_failure_to_act"] is True
    assert summary["metric_labels"]["tool_or_workflow_misrouting"] is True
    assert summary["selection_labels"]["tool_route_selected"] is True
    assert summary["selection_labels"]["launchability_degraded_tool_route"] is True


def test_build_turn_execution_correctness_summary_marks_submission_failure_false_success() -> (
    None
):
    summary = build_turn_execution_correctness_summary(
        completion_gate={
            "decision": "completed",
            "decision_reason": "No blocking effect detected.",
            "safe_to_claim_completion": True,
            "requires_follow_up": False,
        },
        required_effects=[],
        critic_summary={"not_verified_count": 0, "inconclusive_count": 0},
        final_response={
            "completion_claim_detected": True,
            "completion_claim_validated": True,
        },
        workflow_selection={
            "selected_workflow_id": "#V#conversation_turn_execution_workflow",
            "selector_verdict": "rag_selected",
            "selector_source": "workflow_owned",
        },
        workflow_routing_diagnostics={
            "dispatch": {
                "selected_execution_mode": "custom_workflow",
                "dispatch_workflow_id": "#V#conversation_turn_execution_workflow",
                "dispatch_terminal_status": "failed",
                "dispatch_terminal_failure_reason": "workflow_not_runnable",
                "dispatch_terminal_failure_detail": (
                    "Workflow submission failed before any tool or workflow execution."
                ),
                "failure_codes": ["workflow_not_runnable"],
            }
        },
    )

    assert summary["failure_mode"] == "false_completion_gate_state"
    assert summary["overall_outcome"] == "false_success"
    assert summary["metric_labels"]["false_success"] is True


def test_build_turn_execution_correctness_summary_marks_missing_custom_dispatch_false_success() -> (
    None
):
    summary = build_turn_execution_correctness_summary(
        completion_gate={
            "decision": "completed",
            "decision_reason": "No blocking effect detected.",
            "safe_to_claim_completion": True,
            "requires_follow_up": False,
        },
        required_effects=[],
        critic_summary={"not_verified_count": 0, "inconclusive_count": 0},
        final_response={
            "completion_claim_detected": True,
            "completion_claim_validated": True,
        },
        workflow_selection={
            "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
            "selector_verdict": "rag_selected",
            "selector_source": "selector",
        },
        workflow_routing_diagnostics={
            "dispatch": {
                "dispatch_event_count": 0,
                "workflow_handoff_started": False,
            }
        },
    )

    assert summary["failure_mode"] == "false_completion_gate_state"
    assert summary["overall_outcome"] == "false_success"
    assert summary["metric_labels"]["successful_completion"] is False
    assert summary["metric_labels"]["false_success"] is True


def test_build_turn_execution_correctness_summary_marks_answer_evidence_contradiction_false_success() -> (
    None
):
    summary = build_turn_execution_correctness_summary(
        completion_gate={
            "decision": "partial",
            "decision_reason": (
                "Required evidence retrieval returned positive results, but the "
                "answer remained a count-only result summary."
            ),
            "safe_to_claim_completion": False,
            "requires_follow_up": True,
            "blocking_failure_codes": [
                "prompt_required_evidence_positive_results_contradict_low_information_answer"
            ],
            "evidence_payload": {
                "required_evidence_answer_consistency_blocker": {
                    "effect_type": "required_evidence_answer_consistency",
                    "response_surface_kind": "count_only_result_summary",
                }
            },
        },
        required_effects=[],
        critic_summary={"not_verified_count": 0, "inconclusive_count": 0},
        final_response={
            "completion_claim_detected": False,
            "completion_claim_validated": True,
        },
        workflow_selection={
            "selected_workflow_id": "#V#tool_calling_workflow",
            "selector_verdict": "tool_seeking",
            "selector_source": "selector",
        },
        workflow_routing_diagnostics={},
    )

    assert summary["failure_mode"] == "false_completion_claim"
    assert summary["overall_outcome"] == "false_success"
    assert summary["likely_failure_to_act"] is True
    assert summary["metric_labels"]["false_success"] is True
    assert (
        summary["gate_labels"]["required_evidence_answer_consistency_blocked"] is True
    )


def test_turn_execution_record_rejects_search_only_kr_required_tool_run() -> None:
    invocations = [{"tool": "search_concepts", "status": "ok"}]
    ledger = build_required_tool_obligation_ledger(
        required_tools_by_source={"turn_expected_outcome_contract": _KR_REQUIRED_TOOLS},
        invocations=invocations,
        allowed_tools=_KR_REQUIRED_TOOLS,
        method_catalogue={tool_name: {} for tool_name in _KR_REQUIRED_TOOLS},
        max_tool_invocations=1,
    )

    record = build_turn_execution_record(
        request_id="req-kr-search-only",
        session_id="session-1",
        namespace="#V#user@test",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Represent these labels in the Vontology.",
        response_text="Done.",
        interaction_timestamp_utc="2026-05-02T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
        },
        tool_invocations=invocations,
        turn_expected_outcome_contract={"required_tools": _KR_REQUIRED_TOOLS},
        required_tool_obligation_ledger=ledger,
    )

    gate = record["completion_gate"]
    summary = record["execution"]["summary"]
    assert gate["safe_to_claim_completion"] is False
    assert (
        BLOCKER_TOOL_BUDGET_EXHAUSTED_BEFORE_REQUIRED_TOOLS
        in gate["blocking_failure_codes"]
    )
    assert (
        BLOCKER_TOOL_BUDGET_EXHAUSTED_BEFORE_REQUIRED_TOOLS
        in summary["required_tool_obligation_blocking_failure_codes"]
    )
    assert summary["required_tool_obligations"]["unsatisfied_required_tools"] == [
        "create_concepts",
        "add_relationship",
        "upsert_singleton_text_relation",
        "fetch_concept",
        "get_text_relations_summary",
    ]


def test_turn_execution_record_preserves_required_tool_allowed_policy_blocker() -> None:
    required_tools = ["search_concepts", "create_concepts"]
    ledger = build_required_tool_obligation_ledger(
        required_tools_by_source={"turn_expected_outcome_contract": required_tools},
        invocations=[],
        allowed_tools=["search_concepts"],
        method_catalogue={tool_name: {} for tool_name in required_tools},
    )

    record = build_turn_execution_record(
        request_id="req-kr-unallowed",
        session_id="session-1",
        namespace="#V#user@test",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Represent these labels in the Vontology.",
        response_text="I could not write.",
        interaction_timestamp_utc="2026-05-02T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
        },
        tool_invocations=[],
        turn_expected_outcome_contract={"required_tools": required_tools},
        required_tool_obligation_ledger=ledger,
    )

    gate = record["completion_gate"]
    diagnostics = record["workflow_routing_diagnostics"]
    assert gate["safe_to_claim_completion"] is False
    assert (
        BLOCKER_CONTRACT_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY
        in gate["blocking_failure_codes"]
    )
    assert (
        BLOCKER_CONTRACT_REQUIRED_TOOL_NOT_ALLOWED_BY_WORKFLOW_POLICY
        in diagnostics["dispatch"]["required_tool_obligation_blocking_failure_codes"]
    )


def test_turn_execution_record_projects_required_write_payload_validation_blocker() -> (
    None
):
    message = "create_concepts: Missing required field 'parent_id'."

    record = build_turn_execution_record(
        request_id="req-kr-invalid-write",
        session_id="session-1",
        namespace="#V#user@test",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Represent these labels in the Vontology.",
        response_text="The write payload was invalid.",
        interaction_timestamp_utc="2026-05-02T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
        },
        tool_invocations=[],
        turn_expected_outcome_contract={
            "required_tools": ["create_concepts", "fetch_concept"]
        },
        aux_llm_calls=[
            {
                "type": "tool_contract_attempt",
                "stage": "tool_calling.validate",
                "tool_calls": [
                    {
                        "tool": "create_concepts",
                        "payload": {"concepts": [{"name": "Reusable marker"}]},
                    }
                ],
                "validation_errors": [message],
                "diagnostics": [
                    {
                        "tool": "create_concepts",
                        "error_code": "schema_validation_failed",
                        "message": message,
                        "payload": {
                            "concepts": [{"name": "Reusable marker"}],
                        },
                    }
                ],
            }
        ],
    )

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
    assert create_obligation["last_attempt_message"] == message
    assert create_obligation["tool_call_validation_errors"][0]["message"] == message
    assert BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED in (
        summary["required_tool_obligation_blocking_failure_codes"]
    )
    assert BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED in (
        record["completion_gate"]["blocking_failure_codes"]
    )
    assert BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED in (
        record["workflow_routing_diagnostics"]["dispatch"][
            "required_tool_obligation_blocking_failure_codes"
        ]
    )


def test_turn_execution_record_counts_selected_workflow_action_for_required_tool(
    monkeypatch,
) -> None:
    from src.backend.services import tool_metadata_service as metadata_service
    from src.backend.services.tool_metadata_service import ToolMetadata

    monkeypatch.setattr(
        metadata_service,
        "_load_from_vontology",
        lambda: {
            "get_paper_metadata": ToolMetadata(
                tool_name="get_paper_metadata",
                operation_category="read",
                evidence_role="verification",
            )
        },
    )
    metadata_service.invalidate_cache()
    try:
        record = build_turn_execution_record(
            request_id="req-workflow-action-required-tool",
            session_id="session-1",
            namespace="#V#user@test",
            user_id="#V#user",
            org_id="#V#org",
            prompt_text="Represent this paper: https://arxiv.org/abs/2106.03245",
            response_text="Paper concept: #V#paper.",
            interaction_timestamp_utc="2026-06-07T00:00:00Z",
            workflow_routing={
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "verdict": "rag_selected",
                "source": "selector",
            },
            tool_invocations=[],
            selected_workflow_trace={
                "selected_execution_mode": "custom_workflow",
                "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
                "child_workflow_completed": True,
                "child_workflow_final_state": "completed",
                "workflow_execution_summary": {
                    "schema_version": "workflow_execution_summary.v1",
                    "workflow_id": "#V#arxiv_paper_representation_workflow",
                    "completed": True,
                    "terminal_status": "completed",
                    "final_state": "completed",
                    "step_result_envelope_count": 1,
                    "action_started_count": 1,
                    "action_completed_count": 1,
                    "action_success_count": 1,
                    "action_failure_count": 0,
                    "action_unknown_count": 0,
                    "successful_action_ids": ["get_paper_metadata"],
                    "failed_action_ids": [],
                    "action_observations": [
                        {
                            "action_id": "get_paper_metadata",
                            "state_id": "fetch_arxiv_metadata",
                            "action_status": "success",
                            "outcome": "success",
                        }
                    ],
                    "runtime_event_count": 0,
                    "terminal_effect_count": 0,
                    "terminal_effects": [],
                    "durable_side_effect_count": 0,
                    "durable_side_effects": [],
                },
            },
            turn_expected_outcome_contract={
                "required_tools": ["get_paper_metadata"],
            },
            method_catalogue={"get_paper_metadata": {}},
        )
    finally:
        metadata_service.invalidate_cache()

    summary = record["execution"]["summary"]
    ledger = summary["required_tool_obligations"]
    obligation = ledger["obligations"][0]
    assert ledger["satisfied_count"] == 1
    assert ledger["unsatisfied_count"] == 0
    assert ledger["unsatisfied_required_tools"] == []
    assert obligation["tool_name"] == "get_paper_metadata"
    assert obligation["successful_count"] == 1
    assert obligation["blocking_reason"] == ""
    assert obligation["execution_surfaces"][0]["source"] == (
        "workflow_action_execution"
    )
    assert obligation["execution_surfaces"][0]["state_id"] == "fetch_arxiv_metadata"
    assert summary["required_tool_obligation_blocking_failure_codes"] == []
