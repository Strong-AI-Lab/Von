from __future__ import annotations

from typing import Any, Mapping

from src.backend.services.failure_case_intake_service import (
    FAILURE_CASE_INTAKE_COLLECT_ACTION_ID,
    FAILURE_CASE_INTAKE_SCHEMA_VERSION,
    collect_failure_case_intake,
)
from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable.failure_case_prompt_improvement_actions import (
    register_failure_case_prompt_improvement_actions,
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
