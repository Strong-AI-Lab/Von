"""Tests for subworkflow budget diagnosability and non-retryable exhaustion
(JVNAUTOSCI-2502).

A turn that exhausts its subworkflow invocation budget must (a) leave a
ledger naming what consumed the budget, (b) refuse recovery retries that
share the exhausted monotonic counter, and (c) fail fast instead of
re-invoking the selected workflow.
"""

from __future__ import annotations

from unittest.mock import patch

from src.backend.workflows.action_registry import (
    ActionRegistry,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.subworkflow_actions import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    get_subworkflow_invocation_budget_state,
    is_subworkflow_resource_exhaustion_error,
    register_subworkflow_actions,
)


def test_resource_exhaustion_error_classification():
    assert is_subworkflow_resource_exhaustion_error(
        "subworkflow_invocation_budget_exceeded:max_invocations=64"
    )
    assert is_subworkflow_resource_exhaustion_error(
        "subworkflow_invocation_depth_exceeded:max_depth=8"
    )
    assert not is_subworkflow_resource_exhaustion_error("workflow_llm_step_timeout")
    assert not is_subworkflow_resource_exhaustion_error(None)
    assert not is_subworkflow_resource_exhaustion_error("")


def test_budget_state_reports_exhaustion(monkeypatch):
    monkeypatch.setenv("VON_WORKFLOW_SUBWORKFLOW_MAX_INVOCATIONS", "3")
    state = get_subworkflow_invocation_budget_state(
        {"__workflow_subworkflow_invocation_count": 2}
    )
    assert state["invocation_count"] == 2
    assert state["invocation_limit"] == 3
    assert state["remaining"] == 1
    assert state["exhausted"] is False

    state = get_subworkflow_invocation_budget_state(
        {"__workflow_subworkflow_invocation_count": 3}
    )
    assert state["exhausted"] is True
    assert state["remaining"] == 0


def test_budget_exceeded_failure_carries_ledger_summary(monkeypatch):
    registry = ActionRegistry()
    register_subworkflow_actions(
        registry,
        definition_loader=lambda _workflow_id: None,
    )
    monkeypatch.setenv("VON_WORKFLOW_SUBWORKFLOW_MAX_INVOCATIONS", "2")

    context: dict[str, object] = {
        "__workflow_subworkflow_invocation_count": 2,
        "__workflow_subworkflow_invocation_ledger": [
            {
                "index": 1,
                "child_workflow_id": "#V#noisy_child",
                "parent_state_id": "execution",
                "route": "workflow_invoke_subworkflow",
            },
            {
                "index": 2,
                "child_workflow_id": "#V#noisy_child",
                "parent_state_id": "execution",
                "route": "workflow_invoke_subworkflow",
            },
        ],
    }
    execution = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#another_child",
            "__parent_workflow_id": "#V#parent",
            "__parent_state_id": "execution",
        },
        context=context,
        env=WorkflowEnvironment(llm_client=None),
        trace=None,
    )

    assert execution.status == "failed"
    assert "subworkflow_invocation_budget_exceeded" in str(execution.error or "")
    summary = execution.outputs["subworkflow_invocation_ledger_summary"]
    assert summary["total_invocations"] == 2
    assert summary["per_child_workflow"] == {"#V#noisy_child": 2}
    assert summary["per_parent_state"] == {"execution": 2}
    assert execution.outputs["subworkflow_budget_exhausted"] is True
    assert execution.outputs["denied_child_workflow_id"] == "#V#another_child"


def test_persist_failed_turn_execution_record_stamps_terminal_envelope():
    from src.backend.services import turn_execution_record_service as service

    captured: dict = {}
    represented_default = {
        "schema_version": "terminal_outcome_receipt.v1",
        "profile_concept_id": "#V#terminal_outcome_receipt",
        "outcome": "inconclusive",
        "causal_stage": "unknown",
        "cause_code": "represented_critic_receipt_missing",
        "summary": "The represented critic did not produce a complete receipt.",
        "evidence_refs": [{"source": "critic_workflow"}],
        "committed_effects": [],
        "remaining_obligations": [{"effect_id": "effect_terminal_receipt"}],
        "retryability": "now",
        "recovery_affordances": [{"action_type": "alternate_model"}],
        "learning_candidate": None,
        "provenance": {"decision_source": "represented_workflow_default"},
        "redaction_status": "safe_projection",
    }

    def _fake_upsert(*, record, user_id=None, session_id=None, namespace=None, org_id=None):
        captured["record"] = record
        return {"updated": True, "inserted": True, "request_id": record.get("request_id")}

    with patch.object(
        service, "upsert_turn_execution_record_projection", _fake_upsert
    ), patch.object(
        service,
        "_load_represented_terminal_outcome_receipt_default",
        return_value=(
            represented_default,
            {"valid": True},
            {
                "source": "vontology_workflow_validation_default",
                "available": True,
            },
        ),
    ):
        outcome = service.persist_failed_turn_execution_record(
            request_id="req-2502-test",
            terminal_status="cancelled",
            error_text="generate_task_cancelled",
            error_class="CancellationRequested",
            session_id="session-1",
            namespace="#V#test_ns",
            user_id="#V#test_user",
            prompt_text="Show me the last 5 email messages",
            llm_debug_info={
                "aux_llm_calls": [{"type": "workflow_continuation_decision"}],
                "llm_interaction": {
                    "calls": [
                        {
                            "call_id": "req-2502-test:llm:1",
                            "type": "adaptive_turn_model_call",
                            "provider": "openai",
                            "effective_model": "gpt-test",
                            "provider_request_sent": True,
                            "usage": {
                                "prompt_tokens": 100,
                                "completion_tokens": 20,
                            },
                        }
                    ]
                },
                "llm_usage_cost_summary": {
                    "schema_version": "llm_usage_cost_summary.v1",
                    "call_count": 1,
                    "unique_call_count": 1,
                    "estimated_cost": {
                        "status": "unavailable",
                        "amount": None,
                    },
                },
                "tool_invocations": [
                    {
                        "tool": "represented_workflow_test",
                        "status": "error",
                        "error_code": "tool_timeout_after_durable_submission",
                        "effect_status": "partial",
                        "mutation_outcome": "partial",
                        "capability_kind": "represented_workflow",
                        "execution_method": "workflow_execute",
                        "workflow_id": "#V#paper_workflow",
                        "instance_id": "instance-timeout-1",
                    }
                ],
            },
            progress_snapshot={"phase": "recovery_decision"},
        )

    assert outcome["updated"] is True
    record = captured["record"]
    assert record["request_id"] == "req-2502-test"
    assert record["execution_terminal_status"] == "cancelled"
    failure = record["terminal_failure"]
    assert failure["terminal_status"] == "cancelled"
    assert failure["error"] == "generate_task_cancelled"
    assert failure["error_class"] == "CancellationRequested"
    assert failure["progress_snapshot"] == {"phase": "recovery_decision"}
    assert record["terminal_outcome_receipt"]["outcome"] == "inconclusive"
    assert record["terminal_outcome_receipt_authority"] == {
        "source": "vontology_workflow_validation_default",
        "available": True,
    }
    ledger_projection = record["execution"]["tool_observation_ledger"][
        "terminal_outcome_receipt_projection"
    ]
    assert ledger_projection["receipt"]["cause_code"] == (
        "represented_critic_receipt_missing"
    )
    invocation = record["execution"]["tool_invocations"][0]
    assert invocation["tool"] == "represented_workflow_test"
    assert invocation["status"] == "partial"
    assert invocation["error_code"] == (
        "tool_timeout_after_durable_submission"
    )
    assert invocation["instance_id"] == "instance-timeout-1"
    assert record["workflow_selection"]["selected_workflow_id"] == (
        "#V#paper_workflow"
    )
    assert record["execution"]["llm_calls"][0]["call_id"] == (
        "req-2502-test:llm:1"
    )
    assert record["execution"]["llm_calls"][0]["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
    }
    assert record["llm_usage_cost_summary"]["call_count"] == 1


def test_persist_failed_turn_execution_record_requires_request_id():
    from src.backend.services import turn_execution_record_service as service

    outcome = service.persist_failed_turn_execution_record(
        request_id="",
        terminal_status="failed",
    )
    assert outcome == {"updated": False, "reason": "missing_request_id"}
