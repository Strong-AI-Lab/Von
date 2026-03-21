from src.backend.services.turn_execution_record_service import (
    build_workflow_routing_diagnostics,
    _summarise_tool_execution_context,
)


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


def test_worker_unavailable_orchestrator_start_heartbeat_does_not_emit_tool_failure() -> None:
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
                        "stage": "orchestrator_start",
                        "phase": "orchestrator_start",
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


def test_tool_execution_summary_marks_missing_dispatch_boundary_after_tool_selection() -> None:
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
    assert summary["dispatch_terminal_failure_reason"] == "tool_pipeline_setup_exception"
    assert summary["failure_codes"] == [
        "tool_pipeline_setup_exception",
        "tool_dispatch_not_started",
    ]
    assert summary["last_successful_boundary"] == "workflow_terminal"


def test_tool_execution_summary_preserves_custom_workflow_first_step_failure_locality() -> None:
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
    assert summary["zero_tools_executed"] is True
    assert summary["failure_codes"] == []


def test_build_workflow_routing_diagnostics_preserves_selector_exchange_and_dispatch_events() -> None:
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
                                "text": "Selector system prompt",
                                "char_count": 22,
                            },
                        }
                    ],
                    "context_message_count": 1,
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
    assert diagnostics["dispatch"]["selected_execution_mode"] == "tool_pipeline"
    assert diagnostics["dispatch"]["contract_resolution_status"] == "resolved"
    assert diagnostics["dispatch"]["failure_codes"] == ["tool_dispatch_not_started"]
    assert diagnostics["dispatch"]["zero_execution_primary_failure_code"] == (
        "tool_dispatch_not_started"
    )
    assert diagnostics["dispatch"]["last_successful_boundary"] == "contract_resolution"


def test_build_workflow_routing_diagnostics_preserves_local_handoff_failure_details() -> None:
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery={"query": "Run the testing workflow", "matches": [], "candidates": []},
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
