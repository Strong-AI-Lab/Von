"""Contract validation tests for workflow definition identity service."""

from __future__ import annotations

from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from src.backend.workflows.workflow_definition_identity_service import (
    validate_workflow_definition_contract,
)


def test_validate_contract_allows_terminal_failed_sink_without_actions() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#test_terminal_sink_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id="test.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="failed",
                        condition=lambda _ctx: True,
                        reason="forced_failure",
                    ),
                ),
            ),
            "failed": WorkflowStateSpec(state_id="failed", terminal=True),
        },
        termination_states=("failed",),
        purpose="test",
    )

    validation = validate_workflow_definition_contract(
        definition=definition,
        supported_action_ids={"test.action"},
        enforce_supported_actions=True,
    )

    assert validation["valid"] is True
    assert "workflow_step_contract_vacuous" not in (validation.get("errors") or [])
    assert validation.get("vacuous_state_ids") == []


def test_validate_contract_rejects_non_terminal_vacuous_state() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#test_vacuous_state_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="complete",
                        condition=lambda _ctx: True,
                        reason="noop",
                    ),
                ),
            ),
            "complete": WorkflowStateSpec(state_id="complete", terminal=True),
        },
        termination_states=("complete",),
        purpose="test",
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert "workflow_step_contract_vacuous" in (validation.get("errors") or [])
    assert validation.get("vacuous_state_ids") == ["start"]
