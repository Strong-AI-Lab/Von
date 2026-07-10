from __future__ import annotations

from unittest.mock import MagicMock

from src.backend.workflows.action_registry import (
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.turn_execution_runtime_support import (
    run_turn_execution_critic,
)


def _reset_mock_vontology_db(monkeypatch) -> None:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is None:
        return
    for collection_name in ("concepts", "text_relations", "text_values"):
        try:
            db.drop_collection(collection_name)
        except Exception:
            pass


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


def test_turn_execution_critic_accepts_represented_fallback_receipt_input(
    monkeypatch,
) -> None:
    fallback_receipt = {
        "schema_version": "terminal_outcome_receipt.v1",
        "profile_concept_id": "#V#terminal_outcome_receipt",
        "outcome": "inconclusive",
        "causal_stage": "verification",
        "cause_code": "represented_postcondition_critic_unavailable",
        "summary": "The represented critic did not produce a valid judgement.",
        "evidence_refs": [],
        "committed_effects": [],
        "remaining_obligations": [],
        "retryability": "now",
        "recovery_affordances": [{"action_type": "alternate_model"}],
        "learning_candidate": None,
        "provenance": {"decision_source": "represented_workflow_default"},
        "redaction_status": "safe_projection",
    }
    request = WorkflowActionRequest(
        action_id="turn_execution.critic",
        inputs={
            "critic_verdict": {
                "verdict": "inconclusive",
                "terminal_outcome_receipt": fallback_receipt,
            }
        },
        environment=WorkflowEnvironment(llm_client=MagicMock(), user_namespace="ns"),
        data={
            "turn_id": "req-critic-fallback-receipt",
            "conversation_session_id": "sess-critic-fallback-receipt",
            "user_concept_id": "#V#user:test",
            "prompt": "Attempt the represented task.",
            "final_response": "A candidate response.",
            "invocations": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service._load_representation_domain_profiles_from_vontology",
        lambda: {},
    )

    result = run_turn_execution_critic(
        request,
        annotation_component="test",
        annotation_function=(
            "test_turn_execution_critic_accepts_represented_fallback_receipt_input"
        ),
    )

    assert result.status == "success"
    assert result.outputs["terminal_outcome_receipt"] == fallback_receipt
    assert result.outputs["terminal_outcome_receipt_validation"]["valid"] is True
    assert result.outputs["completion_gate_decision"] == "inconclusive"


def test_turn_execution_critic_materialises_gmail_read_tools_as_required_evidence(
    monkeypatch,
) -> None:
    _reset_mock_vontology_db(monkeypatch)

    from src.backend.services.gmail_tool_evidence_contract_vontology_service import (
        bootstrap_gmail_tool_evidence_contract,
    )
    from src.backend.services import tool_metadata_service

    bootstrap_report = bootstrap_gmail_tool_evidence_contract()
    assert bootstrap_report["success"] is True
    tool_metadata_service.invalidate_cache()

    request = WorkflowActionRequest(
        action_id="turn_execution.critic",
        inputs={
            "emit_default_critic_verdict": False,
        },
        environment=WorkflowEnvironment(llm_client=MagicMock(), user_namespace="ns"),
        data={
            "turn_id": "req-critic-gmail-evidence",
            "conversation_session_id": "sess-critic-gmail-evidence",
            "user_concept_id": "#V#user:test",
            "prompt_text": "List my recent Gmail messages and summarise the latest one.",
            "response_text": "I found the latest message details.",
            "completion_report": {
                "workflow_id": "#V#tool_calling_workflow",
                "response_text": "I found the latest message details.",
            },
            "selected_workflow_trace": {
                "selected_workflow_id": "#V#tool_calling_workflow",
            },
            "invocations": [
                {
                    "tool": "gmail_list_messages",
                    "status": "ok",
                    "payload": {"query": "newer_than:7d"},
                    "result_preview": {
                        "messages": [{"id": "msg-1", "subject": "NeurIPS 2026 review"}]
                    },
                    "result": {
                        "messages": [{"id": "msg-1", "subject": "NeurIPS 2026 review"}]
                    },
                },
                {
                    "tool": "gmail_get_message",
                    "status": "ok",
                    "payload": {"message_id": "msg-1"},
                    "result_preview": {
                        "message_id": "msg-1",
                        "subject": "NeurIPS 2026 review",
                    },
                    "result": {
                        "message_id": "msg-1",
                        "subject": "NeurIPS 2026 review",
                        "from": "reviewer@example.org",
                        "snippet": "A new review is available.",
                    },
                },
            ],
            "required_prompt_tools": [
                "gmail_list_messages",
                "gmail_get_message",
            ],
        },
    )

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service._load_representation_domain_profiles_from_vontology",
        lambda: {},
    )

    result = run_turn_execution_critic(
        request,
        annotation_component="test",
        annotation_function=(
            "test_turn_execution_critic_materialises_gmail_read_tools_as_required_evidence"
        ),
    )

    assert result.status == "success"

    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)
    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert [effect.get("required_tools") for effect in required_effects] == [
        ["gmail_list_messages"],
        ["gmail_get_message"],
    ]
    assert all(effect.get("status") == "satisfied" for effect in required_effects)

    tool_metadata_service.invalidate_cache()
