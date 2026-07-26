from __future__ import annotations

import hashlib

import src.backend.services.turn_execution_diagnostics_service as diagnostics_service
from src.backend.services.turn_execution_diagnostics_service import (
    build_workflow_execution_trace_mcp_access_refs,
    get_turn_execution_diagnostics_payload,
)


def test_workflow_trace_refs_include_selected_workflow_use_episode() -> None:
    refs = build_workflow_execution_trace_mcp_access_refs(
        {
            "workflow_routing": {
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "verdict": "rag_selected",
            },
            "aux_llm_calls": [
                {
                    "type": "workflow_execution_trace",
                    "workflow_id": "#V#chat_narration_workflow",
                    "execution_id": "exec-narration",
                },
                {
                    "type": "workflow_use_episode",
                    "workflow_id": "#V#arxiv_paper_representation_workflow",
                    "workflow_instance_id": "wf-instance-selected",
                    "completed": False,
                },
            ],
        }
    )

    assert refs[0]["trace_role"] == "selected_workflow"
    assert refs[0]["workflow_id"] == "#V#arxiv_paper_representation_workflow"
    assert refs[0]["instance_id"] == "wf-instance-selected"
    assert refs[0]["mcp_access"]["arguments"] == {"instance_id": "wf-instance-selected"}
    assert refs[1]["trace_role"] == "auxiliary_workflow"
    assert refs[1]["workflow_id"] == "#V#chat_narration_workflow"
    assert refs[1]["execution_id"] == "exec-narration"


def test_workflow_trace_refs_include_selected_pre_trace_failure() -> None:
    refs = build_workflow_execution_trace_mcp_access_refs(
        {
            "workflow_routing": {
                "workflow_id": "#V#example_selected_workflow",
                "verdict": "rag_selected",
            },
            "selected_workflow_trace": {
                "selected_workflow_id": "#V#example_selected_workflow",
                "workflow_id": "#V#example_selected_workflow",
                "trace_role": "selected_workflow",
                "trace_unavailable": True,
                "trace_unavailable_reason": "workflow_definition_not_runnable",
                "selected_workflow_pre_trace_failure": {
                    "source": "workflow_instance_submission",
                    "status": "submission_failed",
                    "failure_code": "workflow_definition_not_runnable",
                    "failure_detail": "Verified durable workflow submission failed",
                    "workflow_id": "#V#example_selected_workflow",
                },
            },
        }
    )

    assert refs == [
        {
            "execution_id": None,
            "instance_id": None,
            "workflow_id": "#V#example_selected_workflow",
            "trace_role": "selected_workflow",
            "trace_unavailable": True,
            "trace_unavailable_reason": "workflow_definition_not_runnable",
            "selected_workflow_pre_trace_failure": {
                "source": "workflow_instance_submission",
                "status": "submission_failed",
                "failure_code": "workflow_definition_not_runnable",
                "failure_detail": "Verified durable workflow submission failed",
                "workflow_id": "#V#example_selected_workflow",
            },
        }
    ]


def test_turn_execution_diagnostics_exposes_response_surface_reconciliation(
    monkeypatch,
) -> None:
    visible_answer = "Visible response persisted to chat history."
    workflow_response = "Selected workflow response failed during rendering."
    visible_hash = hashlib.sha256(visible_answer.encode("utf-8")).hexdigest()

    monkeypatch.setattr(
        diagnostics_service,
        "_resolve_history_context",
        lambda **_: {
            "user_id": "#V#user",
            "session_id": "session-1",
            "namespace": "#V#user@org",
            "org_id": "#V#org",
            "target_index": 3,
            "target_message": {
                "role": "assistant",
                "content": visible_answer,
                "history_location": {
                    "session_id": "session-1",
                    "history_index": 3,
                },
                "llm_debug_data": {
                    "request_id": "req-1",
                    "turn_execution_diagnostics": {
                        "schema_version": "turn_execution_diagnostics.v1",
                        "request_id": "req-1",
                        "completion_gate": {
                            "decision": "failed",
                            "safe_to_claim_completion": False,
                            "requires_follow_up": True,
                        },
                        "critic_verdict": {"verdict": "pass"},
                    },
                },
            },
            "target_llm_debug": {
                "request_id": "req-1",
                "turn_execution_diagnostics": {
                    "schema_version": "turn_execution_diagnostics.v1",
                    "request_id": "req-1",
                    "completion_gate": {
                        "decision": "failed",
                        "safe_to_claim_completion": False,
                        "requires_follow_up": True,
                    },
                    "critic_verdict": {"verdict": "pass"},
                },
            },
            "prompt_text": "Prompt text",
        },
    )
    monkeypatch.setattr(
        diagnostics_service,
        "_load_turn_execution_record",
        lambda **_: {
            "request_id": "req-1",
            "namespace": "#V#user@org",
            "completion_report": {"response_text": workflow_response},
            "final_response": {"response_sha256": visible_hash},
            "completion_gate": {
                "decision": "failed",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
            },
            "critic": {"verdict": {"verdict": "pass"}},
        },
    )

    payload = get_turn_execution_diagnostics_payload(
        request_id="req-1",
        namespace="#V#user@org",
    )

    assert payload is not None
    surfaces = payload["response_surfaces"]
    assert surfaces["user_visible_response"]["text"] == visible_answer
    assert surfaces["recorded_final_response"]["sha256"] == visible_hash
    assert surfaces["selected_workflow_response"]["text"] == workflow_response
    assert surfaces["evidence_consistency"]["status"] == "inconsistent"
    assert "critic_pass_with_completion_gate_non_success" in (
        surfaces["evidence_consistency"]["disagreement_codes"]
    )


def test_observational_diagnostics_do_not_synthesise_retired_workflow_identity(
    monkeypatch,
) -> None:
    embedded_diagnostics = {
        "schema_version": "turn_execution_diagnostics.v1",
        "record_kind": "observational",
        "request_id": "req-observational",
        "latest_progress": {
            "status": "completed",
            "phase": "completed",
        },
        "phase_history": [
            {"phase": "context_build"},
            {"phase": "model_call"},
            {"phase": "completed"},
        ],
    }
    monkeypatch.setattr(
        diagnostics_service,
        "_resolve_history_context",
        lambda **_: {
            "user_id": "#V#user",
            "session_id": "session-observational",
            "namespace": "#V#user@org",
            "org_id": "#V#org",
            "target_index": 1,
            "target_message": {
                "role": "assistant",
                "content": "Answered.",
            },
            "target_llm_debug": {
                "request_id": "req-observational",
                "turn_execution_diagnostics": embedded_diagnostics,
            },
            "prompt_text": "Answer adaptively.",
        },
    )
    monkeypatch.setattr(
        diagnostics_service,
        "_load_turn_execution_record",
        lambda **_: {
            "schema_version": "turn_execution_record.observational.v1",
            "record_kind": "observational",
            "request_id": "req-observational",
        },
    )

    payload = get_turn_execution_diagnostics_payload(
        request_id="req-observational",
        namespace="#V#user@org",
    )

    assert payload is not None
    assert payload["record_kind"] == "observational"
    assert "workflow_stage_model" not in payload
    assert "workflow_stage_path" not in payload
    assert payload["phase_history"] == embedded_diagnostics["phase_history"]


def test_legacy_diagnostics_still_reconstruct_historical_workflow_identity(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        diagnostics_service,
        "_resolve_history_context",
        lambda **_: {
            "user_id": "#V#user",
            "session_id": "session-legacy",
            "namespace": "#V#user@org",
            "org_id": "#V#org",
            "target_index": 1,
            "target_message": {
                "role": "assistant",
                "content": "Legacy answer.",
            },
            "target_llm_debug": {
                "request_id": "req-legacy",
                "turn_execution_diagnostics": {
                    "schema_version": "turn_execution_diagnostics.v1",
                    "request_id": "req-legacy",
                },
            },
            "prompt_text": "Legacy request.",
        },
    )
    monkeypatch.setattr(
        diagnostics_service,
        "_load_turn_execution_record",
        lambda **_: None,
    )

    payload = get_turn_execution_diagnostics_payload(
        request_id="req-legacy",
        namespace="#V#user@org",
    )

    assert payload is not None
    assert payload["workflow_stage_model"]["workflow_representation_id"] == (
        "#V#conversation_turn_execution_workflow"
    )
    assert payload["workflow_stage_path"]["workflow_representation_id"] == (
        "#V#conversation_turn_execution_workflow"
    )


def test_observational_fallback_does_not_invent_workflow_identity(
    monkeypatch,
) -> None:
    observational_record = {
        "schema_version": "turn_execution_record.observational.v1",
        "record_kind": "observational",
        "request_id": "req-observational-fallback",
        "tool_invocations": [],
    }
    monkeypatch.setattr(
        diagnostics_service,
        "_resolve_history_context",
        lambda **_: {
            "user_id": "#V#user",
            "session_id": "session-observational-fallback",
            "namespace": "#V#user@org",
            "org_id": "#V#org",
            "target_index": 1,
            "target_message": {
                "role": "assistant",
                "content": "Answered without embedded diagnostics.",
            },
            "target_llm_debug": {
                "request_id": "req-observational-fallback",
                "turn_execution_record": observational_record,
            },
            "prompt_text": "Answer adaptively.",
        },
    )
    monkeypatch.setattr(
        diagnostics_service,
        "_load_turn_execution_record",
        lambda **_: observational_record,
    )

    payload = get_turn_execution_diagnostics_payload(
        request_id="req-observational-fallback",
        namespace="#V#user@org",
    )

    assert payload is not None
    assert payload["reconstruction"]["lossy"] is True
    assert "workflow_stage_model" not in payload
    assert "workflow_stage_path" not in payload


def test_turn_execution_diagnostics_repairs_sparse_selector_from_debug_aux(
    monkeypatch,
) -> None:
    selector_prompt_entry = {
        "type": "workflow_selector_prompt",
        "prompt_id": "#V#chat_turn_classifier_prompt",
        "prompt": {"text": "Select a workflow", "char_count": 17},
        "candidate_list": {
            "text": "#V#entity_information_retrieval_workflow",
            "char_count": 40,
        },
        "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
        "candidate_entries": [
            {
                "concept_id": "#V#entity_information_retrieval_workflow",
                "name": "Entity Information Retrieval Workflow",
            }
        ],
        "context_lineage": {"base_context_source": "augmented_context"},
    }
    selector_response_entry = {
        "type": "workflow_selector",
        "stage": "selector_decision",
        "workflow_id": "#V#entity_information_retrieval_workflow",
        "verdict": "rag_selected",
        "prompt_id": "#V#chat_turn_classifier_prompt",
        "response": {
            "text": "#V#entity_information_retrieval_workflow",
            "char_count": 40,
        },
        "candidate_entries": selector_prompt_entry["candidate_entries"],
        "context_lineage": selector_prompt_entry["context_lineage"],
        "reasoning": "The request asks for grounded entity information.",
    }

    monkeypatch.setattr(
        diagnostics_service,
        "_resolve_history_context",
        lambda **_: {
            "user_id": "#V#user",
            "session_id": "session-1",
            "namespace": "#V#user@org",
            "org_id": "#V#org",
            "target_index": 1,
            "target_message": {
                "role": "assistant",
                "content": "Answered.",
                "llm_debug_data": {
                    "request_id": "req-selector",
                    "turn_execution_diagnostics": {
                        "schema_version": "turn_execution_diagnostics.v1",
                        "request_id": "req-selector",
                        "workflow_routing_diagnostics": {
                            "selected_workflow_id": "#V#entity_information_retrieval_workflow",
                            "selector_verdict": "rag_selected",
                            "selector": {
                                "prompt_id": "#V#chat_turn_classifier_prompt",
                                "prompt": None,
                                "candidate_list": None,
                                "response": None,
                            },
                        },
                    },
                },
            },
            "target_llm_debug": {
                "request_id": "req-selector",
                "workflow_discovery": {
                    "candidates": selector_prompt_entry["candidate_entries"],
                    "candidate_count": 1,
                    "matches": ["#V#entity_information_retrieval_workflow"],
                },
                "workflow_routing": {
                    "workflow_id": "#V#entity_information_retrieval_workflow",
                    "verdict": "rag_selected",
                    "prompt_id": "#V#chat_turn_classifier_prompt",
                    "source": "selector",
                    "reasoning": "The request asks for grounded entity information.",
                },
                "aux_llm_calls": [selector_prompt_entry, selector_response_entry],
                "turn_execution_diagnostics": {
                    "schema_version": "turn_execution_diagnostics.v1",
                    "request_id": "req-selector",
                    "workflow_routing_diagnostics": {
                        "selected_workflow_id": "#V#entity_information_retrieval_workflow",
                        "selector_verdict": "rag_selected",
                        "selector": {
                            "prompt_id": "#V#chat_turn_classifier_prompt",
                            "prompt": None,
                            "candidate_list": None,
                            "response": None,
                        },
                    },
                },
            },
            "prompt_text": "Who am I?",
        },
    )
    monkeypatch.setattr(diagnostics_service, "_load_turn_execution_record", lambda **_: None)

    payload = get_turn_execution_diagnostics_payload(
        request_id="req-selector",
        namespace="#V#user@org",
    )

    assert payload is not None
    selector = payload["workflow_routing_diagnostics"]["selector"]
    assert selector["prompt"]["text"] == "Select a workflow"
    assert selector["candidate_list"]["text"] == (
        "#V#entity_information_retrieval_workflow"
    )
    assert selector["response"]["text"] == "#V#entity_information_retrieval_workflow"
    assert selector["requested_prompt_ids"] == ["#V#chat_turn_classifier_prompt"]
    assert selector["candidate_entries"][0]["concept_id"] == (
        "#V#entity_information_retrieval_workflow"
    )
    assert selector["context_lineage"] == {"base_context_source": "augmented_context"}
    assert selector["telemetry_completeness"]["missing_fields"] == []


def test_turn_execution_diagnostics_marks_sparse_selector_telemetry(monkeypatch) -> None:
    monkeypatch.setattr(
        diagnostics_service,
        "_resolve_history_context",
        lambda **_: {
            "user_id": "#V#user",
            "session_id": "session-1",
            "namespace": "#V#user@org",
            "org_id": "#V#org",
            "target_index": 1,
            "target_message": {"role": "assistant", "content": "Answered."},
            "target_llm_debug": {
                "request_id": "req-sparse-selector",
                "workflow_routing": {
                    "workflow_id": "#V#tool_calling_workflow",
                    "verdict": "rag_selected",
                    "prompt_id": "#V#chat_turn_classifier_prompt",
                    "source": "selector",
                },
                "turn_execution_diagnostics": {
                    "schema_version": "turn_execution_diagnostics.v1",
                    "request_id": "req-sparse-selector",
                    "workflow_routing_diagnostics": {
                        "selected_workflow_id": "#V#tool_calling_workflow",
                        "selector_verdict": "rag_selected",
                        "selector": {
                            "prompt_id": "#V#chat_turn_classifier_prompt",
                            "prompt": None,
                            "candidate_list": None,
                            "response": None,
                        },
                    },
                },
            },
            "prompt_text": "Who am I?",
        },
    )
    monkeypatch.setattr(diagnostics_service, "_load_turn_execution_record", lambda **_: None)

    payload = get_turn_execution_diagnostics_payload(
        request_id="req-sparse-selector",
        namespace="#V#user@org",
    )

    assert payload is not None
    completeness = payload["workflow_routing_diagnostics"]["selector"][
        "telemetry_completeness"
    ]
    assert completeness["prompt_present"] is False
    assert completeness["candidate_list_present"] is False
    assert completeness["response_present"] is False
    assert completeness["response_absence_reason"] == (
        "selector_aux_telemetry_unavailable"
    )
    assert set(completeness["missing_fields"]) >= {
        "prompt",
        "candidate_list",
        "response",
    }
