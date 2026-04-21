from __future__ import annotations

from unittest.mock import MagicMock

from src.backend.workflows.action_registry import WorkflowActionRequest, WorkflowEnvironment
from src.backend.workflows.durable.turn_execution_runtime_support import (
    run_turn_execution_critic,
)


def test_turn_execution_critic_can_build_evidence_without_default_critic_verdict(
    monkeypatch,
) -> None:
    request = WorkflowActionRequest(
        action_id="turn_execution.critic",
        inputs={
            "emit_default_critic_verdict": False,
        },
        environment=WorkflowEnvironment(llm_client=MagicMock(), user_namespace="ns"),
        data={
            "turn_id": "req-critic-evidence",
            "conversation_session_id": "sess-critic-evidence",
            "user_concept_id": "#V#user:test",
            "prompt_text": "Search Jira for my open tasks and tell me what you find.",
            "response_text": "I couldn't find any tasks.",
            "completion_report": {
                "workflow_id": "#V#tool_calling_workflow",
                "response_text": "I couldn't find any tasks.",
            },
            "selected_workflow_trace": {
                "selected_workflow_id": "#V#tool_calling_workflow",
            },
            "invocations": [
                {
                    "tool": "jira_search",
                    "status": "ok",
                    "payload": {"jql": "assignee = currentUser()"},
                    "result_preview": {"items": [{"key": "JVNAUTOSCI-1"}]},
                    "result": {"items": [{"key": "JVNAUTOSCI-1"}]},
                }
            ],
            "required_prompt_tools": ["jira_search"],
        },
    )

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service._load_representation_domain_profiles_from_vontology",
        lambda: {},
    )

    result = run_turn_execution_critic(
        request,
        annotation_component="test",
        annotation_function="test_turn_execution_critic_can_build_evidence_without_default_critic_verdict",
    )

    assert result.status == "success"
    assert result.outputs.get("critic_verdict") is None

    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)
    execution_summary = record.get("execution", {}).get("summary", {})
    assert execution_summary.get("required_evidence_answer_consistency_source") is None

    completion_gate = record.get("completion_gate") or {}
    evidence_payload = completion_gate.get("evidence_payload") or {}
    assert evidence_payload.get("required_evidence_answer_consistency_blocker") is None

    evidence_bundle = result.outputs.get("turn_execution_critic_evidence_bundle")
    assert isinstance(evidence_bundle, dict)
    assert evidence_bundle.get("schema_version") == (
        "turn_execution_postcondition_critic_bundle.v1"
    )