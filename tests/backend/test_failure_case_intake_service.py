from __future__ import annotations

import hashlib
from typing import Any, Mapping

from src.backend.services.failure_case_intake_service import (
    FAILURE_CASE_INTAKE_COLLECT_ACTION_ID,
    FAILURE_CASE_INTAKE_SCHEMA_VERSION,
    FAILURE_CASE_REFERENCE_RESOLVE_ACTION_ID,
    FAILURE_CASE_REFERENCE_SCHEMA_VERSION,
    collect_failure_case_intake,
    resolve_failure_case_reference,
)
from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable.failure_case_prompt_improvement_actions import (
    register_failure_case_prompt_improvement_actions,
)
from src.backend.workflows.workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
)
from src.backend.workflows.workflow_definition_identity_service import (
    validate_workflow_definition_contract,
)


class RecordingInvoker:
    def __init__(self, responses: Mapping[str, Mapping[str, Any]]) -> None:
        self.responses = {key: dict(value) for key, value in responses.items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke(self, tool_name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_name, dict(payload)))
        return dict(
            self.responses.get(tool_name, {"success": False, "error": "missing"})
        )


def test_collect_failure_case_intake_uses_canonical_sources_for_unsigned_ref() -> None:
    namespace = "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    request_id = "b2396486-9d40-4e04-a843-744ca8fa7a3b"
    conversation_ref = {
        "kind": "von_conversation_ref",
        "conversation_ref": {
            "session_id": "ff9be41d-28f8-4864-ab0b-8c201e3152d0",
            "user_concept_id": "#V#michael_witbrock",
            "namespace": namespace,
            "organisation_concept_id": "university_of_auckland_strong_ai_lab",
            "include_legacy": False,
        },
        "chat_history_lookup": {
            "user_id": "#V#michael_witbrock",
            "session_id": "ff9be41d-28f8-4864-ab0b-8c201e3152d0",
            "namespace": namespace,
            "include_legacy": False,
        },
    }
    invoker = RecordingInvoker(
        {
            "chat_history_get_segments": {
                "success": True,
                "session_id": "ff9be41d-28f8-4864-ab0b-8c201e3152d0",
                "chat_session_id": "ff9be41d-28f8-4864-ab0b-8c201e3152d0",
                "namespace": namespace,
                "organisation_concept_id": "university_of_auckland_strong_ai_lab",
                "access_mode": "session",
                "identifier_binding": "explicit_session",
                "segment_count": 1,
                "segments": [
                    [
                        {"role": "user", "content": "Why did the Gemma turn fail?"},
                        {
                            "role": "assistant",
                            "content": "It produced an unhelpful answer.",
                            "history_location": {
                                "session_id": "ff9be41d-28f8-4864-ab0b-8c201e3152d0",
                                "history_index": 7,
                            },
                            "llm_debug_data": {
                                "request_id": request_id,
                                "model": "gemma-3-27b-it",
                                "workflow_selection": {
                                    "selected_workflow_id": "prompt_improvement_failure",
                                    "selector_verdict": "selected",
                                },
                                "selected_prompt_id": "#V#gemma_failure_prompt_v1",
                            },
                        },
                    ]
                ],
            },
            "turn_execution_get_diagnostics": {
                "success": True,
                "request_id": request_id,
                "namespace": namespace,
                "diagnostics_source": "mongo.turn_execution_records",
                "history_location": {
                    "session_id": "ff9be41d-28f8-4864-ab0b-8c201e3152d0",
                    "history_index": 7,
                },
                "workflow_selection": {
                    "selected_workflow_id": "prompt_improvement_failure",
                    "selector_verdict": "selected",
                },
                "completion_gate": {
                    "decision": "needs_replay",
                    "safe_to_claim_completion": False,
                },
                "critic": {"summary": {"verdict": "fail"}},
                "mcp_access": {
                    "chat_history_get_debug_entry": {
                        "arguments": {
                            "session_id": "ff9be41d-28f8-4864-ab0b-8c201e3152d0",
                            "namespace": namespace,
                            "user_concept_id": "#V#michael_witbrock",
                            "history_index": 7,
                        }
                    }
                },
            },
            "chat_history_get_debug_entry": {
                "success": True,
                "history_location": {
                    "session_id": "ff9be41d-28f8-4864-ab0b-8c201e3152d0",
                    "history_index": 7,
                },
                "llm_debug_data": {
                    "request_id": request_id,
                    "prompt_text": "Prompt text that Gemma actually saw.",
                    "model": "gemma-3-27b-it",
                    "prompt_variant_selection": {
                        "base_prompt_concept_id": "#V#failure_case_prompt",
                        "selected_prompt_concept_id": "#V#gemma_failure_prompt_v1",
                        "matched_model_family": "gemma",
                    },
                },
            },
            "turn_execution_get": {
                "success": True,
                "request_id": request_id,
                "final_response": {"text": "It produced an unhelpful answer."},
                "workflow_selection": {
                    "selected_workflow_id": "prompt_improvement_failure",
                    "selector_verdict": "selected",
                },
                "execution": {
                    "tool_invocations": [
                        {"tool_name": "chat_history_get_segments", "status": "success"},
                        {"tool_name": "gmail_search", "status": "failed"},
                    ],
                    "summary": {
                        "workflow_required_effects_required_tools": [
                            "chat_history_get_segments",
                            "turn_execution_get_diagnostics",
                        ]
                    },
                },
                "required_effects": [
                    {"id": "effect-1", "required_tools": ["gmail_search"]}
                ],
            },
            "turn_execution_list": {
                "success": True,
                "total": 1,
                "items": [{"request_id": request_id}],
            },
        }
    )

    payload = collect_failure_case_intake(
        conversation_ref=conversation_ref,
        request_id=request_id,
        target_model="gemma-3-27b-it",
        comparator_model="gpt-5.4-mini",
        mcp_invoker=invoker,
    )

    assert payload["schema_version"] == FAILURE_CASE_INTAKE_SCHEMA_VERSION
    assert payload["success"] is True
    assert payload["turn"]["prompt"]["text"] == "Prompt text that Gemma actually saw."
    assert payload["turn"]["user_visible_response"]["text"] == (
        "It produced an unhelpful answer."
    )
    assert payload["model"]["primary_model"] == "gemma-3-27b-it"
    assert payload["model"]["target_model_matches_primary"] is True
    assert payload["workflow"]["selected_workflow_id"] == "prompt_improvement_failure"
    assert "#V#gemma_failure_prompt_v1" in payload["prompt_metadata"]["prompt_ids"]
    assert payload["tool_ledger"]["counts"] == {
        "total": 2,
        "success": 1,
        "failed": 1,
        "pending": 0,
        "unknown": 0,
    }
    assert payload["completion_gate"]["decision"] == "needs_replay"
    assert payload["critic"]["summary"]["verdict"] == "fail"
    assert payload["policy_boundary"]["classification_performed"] is False

    first_tool, first_args = invoker.calls[0]
    assert first_tool == "chat_history_get_segments"
    assert first_args["session_id"] == "ff9be41d-28f8-4864-ab0b-8c201e3152d0"
    assert "conversation_ref" not in first_args
    called_tools = [tool for tool, _ in invoker.calls]
    assert called_tools == [
        "chat_history_get_segments",
        "turn_execution_get_diagnostics",
        "chat_history_get_debug_entry",
        "turn_execution_get",
        "turn_execution_list",
    ]


def test_collect_failure_case_intake_reconciles_visible_and_workflow_surfaces() -> (
    None
):
    namespace = "#V#user@org"
    request_id = "req-gpt-comparator"
    visible_answer = (
        "Here are your six most recent Gmail messages:\n"
        "1. Meeting Summary - Michael - 2026-05-13\n"
        "2. Meeting Summary - Yue - 2026-05-13"
    )
    workflow_error = (
        "Invalid request to OpenAI API: context_length_exceeded while rendering "
        "the selected workflow response."
    )
    visible_hash = hashlib.sha256(visible_answer.encode("utf-8")).hexdigest()
    workflow_hash = hashlib.sha256(workflow_error.encode("utf-8")).hexdigest()
    invoker = RecordingInvoker(
        {
            "chat_history_get_segments": {
                "success": True,
                "session_id": "session-1",
                "chat_session_id": "session-1",
                "namespace": namespace,
                "segment_count": 1,
                "segments": [
                    [
                        {"role": "user", "content": "List my last six email messages."},
                        {
                            "role": "assistant",
                            "content": visible_answer,
                            "history_location": {
                                "session_id": "session-1",
                                "history_index": 41,
                            },
                            "llm_debug_data": {
                                "request_id": request_id,
                                "model": "gpt-5.4-mini",
                                "workflow_selection": {
                                    "selected_workflow_id": (
                                        "#V#general_mail_review_workflow"
                                    ),
                                    "selector_verdict": "rag_selected",
                                },
                            },
                        },
                    ]
                ],
            },
            "turn_execution_get_diagnostics": {
                "success": True,
                "request_id": request_id,
                "namespace": namespace,
                "diagnostics_source": "chat_history.llm_debug_data",
                "history_location": {
                    "session_id": "session-1",
                    "history_index": 41,
                },
                "prompt_preview": "List my last six email messages.",
                "workflow_selection": {
                    "selected_workflow_id": "#V#general_mail_review_workflow",
                    "selector_verdict": "rag_selected",
                },
                "completion_gate": {
                    "decision": "failed",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "evidence_payload": {
                        "blocking_failure_codes": ["child_workflow_failed"],
                        "execution_signal_blocker": {
                            "failure_code": "child_workflow_failed",
                            "status": "not_satisfied",
                            "status_reason": workflow_error,
                            "workflow_id": "#V#general_mail_review_workflow",
                        },
                    },
                },
                "critic_verdict": {
                    "verdict": "pass",
                    "assessment_summary": (
                        "The assistant correctly stated that Gmail results were "
                        "not available in readable form."
                    ),
                },
                "response_surfaces": {
                    "schema_version": "turn_response_surfaces.v1",
                    "selected_workflow_response": {
                        "kind": "selected_workflow_response",
                        "source": (
                            "turn_execution_record.completion_report.response_text"
                        ),
                        "text_available": True,
                        "text": workflow_error,
                        "char_count": len(workflow_error),
                        "sha256": workflow_hash,
                        "truncated": False,
                    },
                },
            },
            "turn_execution_get": {
                "success": True,
                "request_id": request_id,
                "final_response": {
                    "response_sha256": visible_hash,
                    "completion_claim_detected": False,
                    "completion_claim_validated": True,
                },
                "workflow_selection": {
                    "selected_workflow_id": "#V#general_mail_review_workflow",
                    "selector_verdict": "rag_selected",
                },
                "critic": {
                    "verdict": {
                        "verdict": "pass",
                        "assessment_summary": (
                            "The assistant correctly stated that Gmail results "
                            "were not available in readable form."
                        ),
                    }
                },
                "completion_gate": {
                    "decision": "failed",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                },
                "execution": {
                    "tool_invocations": [
                        {"tool_name": "gmail_list_messages", "status": "success"},
                        {"tool_name": "gmail_get_message", "status": "success"},
                    ]
                },
            },
            "turn_execution_list": {
                "success": True,
                "total": 1,
                "items": [{"request_id": request_id}],
            },
        }
    )

    payload = collect_failure_case_intake(
        session_id="session-1",
        namespace=namespace,
        request_id=request_id,
        target_model="gpt-5.4-mini",
        mcp_invoker=invoker,
    )

    assert payload["success"] is True
    assert payload["turn"]["user_visible_response"]["text"] == visible_answer
    surfaces = payload["response_surfaces"]
    assert surfaces["user_visible_response"]["source"] == (
        "chat_history.target_message.content"
    )
    assert surfaces["recorded_final_response"]["sha256"] == visible_hash
    assert surfaces["selected_workflow_response"]["text"] == workflow_error
    assert surfaces["evidence_consistency"]["agreement"] == {
        "user_visible_matches_recorded_final_response": True,
        "user_visible_matches_selected_workflow_response": False,
        "recorded_final_response_matches_selected_workflow_response": False,
    }
    assert set(surfaces["evidence_consistency"]["disagreement_codes"]) == {
        "user_visible_response_differs_from_selected_workflow_response",
        "turn_record_final_response_differs_from_selected_workflow_response",
        "critic_pass_with_completion_gate_non_success",
    }
    assert surfaces["policy_boundary"]["answer_correctness_classified"] is False


def test_collect_failure_case_intake_requires_unique_turn_without_request_id() -> None:
    invoker = RecordingInvoker(
        {
            "chat_history_get_segments": {
                "success": True,
                "session_id": "session-1",
                "segment_count": 1,
                "segments": [
                    [
                        {"role": "user", "content": "First"},
                        {
                            "role": "assistant",
                            "content": "First response",
                            "llm_debug_data": {
                                "request_id": "req-1",
                                "model": "gemma-3-27b-it",
                            },
                        },
                        {"role": "user", "content": "Second"},
                        {
                            "role": "assistant",
                            "content": "Second response",
                            "llm_debug_data": {
                                "request_id": "req-2",
                                "model": "gemma-3-27b-it",
                            },
                        },
                    ]
                ],
            }
        }
    )

    payload = collect_failure_case_intake(
        session_id="session-1",
        namespace="#V#user@org",
        target_model="gemma-3-27b-it",
        mcp_invoker=invoker,
    )

    assert payload["success"] is False
    assert payload["error_code"] == "request_id_required_for_failure_case_intake"
    assert payload["request_resolution"]["filtered_candidate_count"] == 2
    assert [candidate["request_id"] for candidate in payload["turn_candidates"]] == [
        "req-1",
        "req-2",
    ]
    assert [tool for tool, _ in invoker.calls] == ["chat_history_get_segments"]


def test_resolve_failure_case_reference_selects_latest_prior_failed_turn() -> None:
    invoker = RecordingInvoker(
        {
            "chat_history_get_segments": {
                "success": True,
                "session_id": "session-1",
                "chat_session_id": "session-1",
                "namespace": "#V#user@org",
                "segment_count": 1,
                "segments": [
                    [
                        {"role": "user", "content": "Do the first thing"},
                        {
                            "role": "assistant",
                            "content": "First thing done",
                            "llm_debug_data": {
                                "request_id": "req-success",
                                "model": "gemma-3-27b-it",
                                "workflow_selection": {
                                    "selected_workflow_id": "#V#some_workflow"
                                },
                            },
                        },
                        {"role": "user", "content": "Do the second thing"},
                        {
                            "role": "assistant",
                            "content": "I could not complete it.",
                            "llm_debug_data": {
                                "request_id": "req-failure",
                                "model": "gemma-3-27b-it",
                                "workflow_selection": {
                                    "selected_workflow_id": "#V#some_workflow"
                                },
                            },
                        },
                    ]
                ],
            },
            "turn_execution_list": {
                "success": True,
                "total": 2,
                "items": [
                    {"request_id": "req-success", "decision": "success"},
                    {
                        "request_id": "req-failure",
                        "decision": "partial",
                        "blocking_effect_ids": ["effect-answer-contract"],
                    },
                ],
            },
        }
    )

    payload = resolve_failure_case_reference(
        session_id="session-1",
        namespace="#V#user@org",
        current_request_id="req-current",
        reference_mode="that_failure_case",
        reference_phrase="try to fix that failure case",
        mcp_invoker=invoker,
    )

    assert payload["schema_version"] == FAILURE_CASE_REFERENCE_SCHEMA_VERSION
    assert payload["success"] is True
    assert payload["failure_case_reference_resolved"] is True
    assert payload["resolved_request_id"] == "req-failure"
    assert payload["selected_candidate"]["failure_reference_status"][
        "is_failure_candidate"
    ]
    assert (
        "decision:partial"
        in payload["selected_candidate"]["failure_reference_status"]["failure_signals"]
    )
    assert payload["policy_boundary"]["utterance_classification_performed"] is False
    assert [tool for tool, _ in invoker.calls] == [
        "chat_history_get_segments",
        "turn_execution_list",
    ]


def test_collect_failure_case_intake_can_start_from_same_conversation_reference() -> (
    None
):
    invoker = RecordingInvoker(
        {
            "chat_history_get_segments": {
                "success": True,
                "session_id": "session-1",
                "chat_session_id": "session-1",
                "namespace": "#V#user@org",
                "segment_count": 1,
                "segments": [
                    [
                        {"role": "user", "content": "List my recent messages"},
                        {
                            "role": "assistant",
                            "content": "I retrieved them but did not render the list.",
                            "history_location": {
                                "session_id": "session-1",
                                "history_index": 3,
                            },
                            "llm_debug_data": {
                                "request_id": "req-failure",
                                "model": "gemma-3-27b-it",
                                "workflow_selection": {
                                    "selected_workflow_id": "#V#mail_workflow",
                                    "selector_verdict": "rag_selected",
                                },
                                "selected_prompt_id": "#V#mail_prompt",
                            },
                        },
                        {"role": "user", "content": "try to fix that failure case"},
                    ]
                ],
            },
            "turn_execution_list": {
                "success": True,
                "total": 1,
                "items": [
                    {
                        "request_id": "req-failure",
                        "decision": "partial",
                        "blocking_effect_ids": ["effect-answer-contract"],
                    }
                ],
            },
            "turn_execution_get_diagnostics": {
                "success": True,
                "request_id": "req-failure",
                "namespace": "#V#user@org",
                "diagnostics_source": "mongo.turn_execution_records",
                "workflow_selection": {
                    "selected_workflow_id": "#V#mail_workflow",
                    "selector_verdict": "rag_selected",
                },
                "completion_gate": {"decision": "partial"},
            },
            "turn_execution_get": {
                "success": True,
                "request_id": "req-failure",
                "final_response": {
                    "text": "I retrieved them but did not render the list."
                },
                "workflow_selection": {
                    "selected_workflow_id": "#V#mail_workflow",
                    "selector_verdict": "rag_selected",
                },
            },
        }
    )

    payload = collect_failure_case_intake(
        session_id="session-1",
        namespace="#V#user@org",
        current_request_id="req-current",
        reference_mode="latest_prior_failure",
        reference_phrase="try to fix that failure case",
        mcp_invoker=invoker,
    )

    assert payload["success"] is True
    assert payload["failure_case_intake_collected"] is True
    assert payload["request_id"] == "req-failure"
    assert payload["request_resolution"]["provided_request_id"] is None
    assert payload["request_resolution"]["reference_resolution"][
        "resolved_request_id"
    ] == ("req-failure")
    assert payload["turn"]["prompt"]["text"] == "List my recent messages"
    assert payload["workflow"]["selected_workflow_id"] == "#V#mail_workflow"
    assert [tool for tool, _ in invoker.calls].count("chat_history_get_segments") == 2
    assert all(
        args.get("request_id") != "req-current"
        for _, args in invoker.calls
        if isinstance(args, dict)
    )


def test_failure_case_intake_action_delegates_to_service(monkeypatch) -> None:
    from src.backend.workflows.durable import failure_case_prompt_improvement_actions

    captured: dict[str, Any] = {}

    def fake_collect_failure_case_intake(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"success": True, "request_id": kwargs.get("request_id")}

    monkeypatch.setattr(
        failure_case_prompt_improvement_actions,
        "collect_failure_case_intake",
        fake_collect_failure_case_intake,
    )

    registry = ActionRegistry()
    register_failure_case_prompt_improvement_actions(registry)
    context = {"request_id": "req-from-context"}
    result = registry.execute(
        FAILURE_CASE_INTAKE_COLLECT_ACTION_ID,
        inputs={"conversation_ref": {"session_id": "session-1"}},
        context=context,
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=object(),
            user_namespace="#V#user@org",
            user_concept_id="#V#user",
            org_concept_id="#V#org",
        ),
    )

    assert result.status == "success"
    assert result.outputs["request_id"] == "req-from-context"
    assert captured["namespace"] == "#V#user@org"
    assert captured["user_concept_id"] == "#V#user"
    assert captured["organisation_concept_id"] == "#V#org"
    assert captured["mcp_invoker"] is not None


def test_failure_case_intake_action_treats_context_request_id_as_current_for_reference(
    monkeypatch,
) -> None:
    from src.backend.workflows.durable import failure_case_prompt_improvement_actions

    captured: dict[str, Any] = {}

    def fake_collect_failure_case_intake(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"success": True, "request_id": "req-failure"}

    monkeypatch.setattr(
        failure_case_prompt_improvement_actions,
        "collect_failure_case_intake",
        fake_collect_failure_case_intake,
    )

    registry = ActionRegistry()
    register_failure_case_prompt_improvement_actions(registry)
    result = registry.execute(
        FAILURE_CASE_INTAKE_COLLECT_ACTION_ID,
        inputs={
            "conversation_ref": {"session_id": "session-1"},
            "reference_mode": "latest_prior_failure",
        },
        context={"request_id": "req-current"},
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=object(),
            user_namespace="#V#user@org",
        ),
    )

    assert result.status == "success"
    assert captured["request_id"] is None
    assert captured["current_request_id"] == "req-current"
    assert captured["reference_mode"] == "latest_prior_failure"


def test_failure_case_reference_action_delegates_to_resolver(monkeypatch) -> None:
    from src.backend.workflows.durable import failure_case_prompt_improvement_actions

    captured: dict[str, Any] = {}

    def fake_resolve_failure_case_reference(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"success": True, "resolved_request_id": "req-failure"}

    monkeypatch.setattr(
        failure_case_prompt_improvement_actions,
        "resolve_failure_case_reference",
        fake_resolve_failure_case_reference,
    )

    registry = ActionRegistry()
    register_failure_case_prompt_improvement_actions(registry)
    result = registry.execute(
        FAILURE_CASE_REFERENCE_RESOLVE_ACTION_ID,
        inputs={"conversation_ref": {"session_id": "session-1"}},
        context={"request_id": "req-current"},
        env=WorkflowEnvironment(
            llm_client=None,
            gateway=object(),
            user_namespace="#V#user@org",
        ),
    )

    assert result.status == "success"
    assert result.outputs["resolved_request_id"] == "req-failure"
    assert captured["current_request_id"] == "req-current"


def test_failure_case_prompt_improvement_workflow_spec_uses_reference_context() -> None:
    spec = {
        "workflow_id": "#V#failure_case_prompt_improvement_workflow",
        "workflow_name": "Failure case prompt improvement workflow",
        "workflow_description": (
            "Starts replay-backed improvement from a prior failed or incomplete "
            "turn by collecting grounded failure-case evidence. Same-conversation "
            "reference interpretation is supplied by represented routing metadata; "
            "the deterministic step only resolves the selected latest-prior-failure "
            "mode and collects intake."
        ),
        "parent_type_id": "#V#durable_workflow",
        "initial_state_key": "collect_failure_case_intake",
        "steps": [
            {
                "state_id": "collect_failure_case_intake",
                "state_key": "collect_failure_case_intake",
                "action_id": FAILURE_CASE_INTAKE_COLLECT_ACTION_ID,
                "execution_mode": "deterministic",
                "static_input_bindings": [
                    {"tool_param": "reference_mode", "value": "latest_prior_failure"},
                    {"tool_param": "history_tail_limit", "value": 80},
                    {"tool_param": "max_reference_candidates", "value": 24},
                    {"tool_param": "max_text_chars", "value": 4000},
                ],
                "context_input_mappings": [
                    {
                        "tool_param": "request_id",
                        "context_key": "failure_request_id",
                        "required": False,
                    },
                    {
                        "tool_param": "conversation_ref",
                        "context_key": "conversation_ref",
                        "required": False,
                    },
                    {
                        "tool_param": "chat_history_lookup",
                        "context_key": "chat_history_lookup",
                        "required": False,
                    },
                    {
                        "tool_param": "session_id",
                        "context_key": "session_id",
                        "required": False,
                    },
                    {
                        "tool_param": "current_request_id",
                        "context_key": "request_id",
                        "required": False,
                    },
                    {
                        "tool_param": "target_model",
                        "context_key": "target_model",
                        "required": False,
                    },
                    {
                        "tool_param": "comparator_model",
                        "context_key": "comparator_model",
                        "required": False,
                    },
                    {
                        "tool_param": "workflow_id",
                        "context_key": "failed_workflow_id",
                        "required": False,
                    },
                    {
                        "tool_param": "namespace",
                        "context_key": "namespace",
                        "required": False,
                    },
                    {
                        "tool_param": "user_concept_id",
                        "context_key": "user_concept_id",
                        "required": False,
                    },
                    {
                        "tool_param": "organisation_concept_id",
                        "context_key": "organisation_concept_id",
                        "required": False,
                    },
                    {
                        "tool_param": "reference_phrase",
                        "context_key": "prompt",
                        "required": False,
                    },
                ],
                "writes_context_keys": [
                    "failure_case_intake_collected",
                    "request_id",
                    "request_resolution",
                    "turn",
                    "workflow",
                    "prompt",
                    "completion_gate",
                    "critic",
                    "response_surfaces",
                    "policy_boundary",
                ],
                "tool_output_context_mappings": [
                    {
                        "tool_output_field": "request_id",
                        "context_key": "failure_request_id",
                    },
                    {
                        "tool_output_field": "request_resolution.reference_resolution",
                        "context_key": "failure_case_reference_resolution",
                    },
                ],
                "next_state_key": "completed",
                "on_failure_state_key": "failed",
            },
            {"state_id": "completed", "state_key": "completed", "terminal": True},
            {"state_id": "failed", "state_key": "failed", "terminal": True},
        ],
    }

    definition = build_workflow_definition_from_authoring_spec(spec)
    start = definition.states["collect_failure_case_intake"]
    action = start.actions[0]

    assert action.inputs["reference_mode"] == "latest_prior_failure"
    assert action.inputs["history_tail_limit"] == 80
    assert action.inputs["request_id"] == {
        "$context_key": "failure_request_id",
        "$required": False,
    }
    assert action.inputs["current_request_id"] == {
        "$context_key": "request_id",
        "$required": False,
    }
    assert action.inputs["target_model"] == {
        "$context_key": "target_model",
        "$required": False,
    }

    validation = validate_workflow_definition_contract(
        definition=definition,
        supported_action_ids=(FAILURE_CASE_INTAKE_COLLECT_ACTION_ID,),
        enforce_supported_actions=True,
    )
    assert validation["valid"] is True
