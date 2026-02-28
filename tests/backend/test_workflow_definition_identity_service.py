"""Contract validation tests for workflow definition identity service."""

from __future__ import annotations

from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from src.backend.workflows.subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    build_subworkflow_contract,
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


def _child_workflow_definition(
    *,
    workflow_id: str = "#V#child_workflow",
    required_inputs: tuple[str, ...] = ("child_input",),
    produced_outputs: tuple[str, ...] = ("child_output",),
) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="child_start",
        states={
            "child_start": WorkflowStateSpec(
                state_id="child_start",
                actions=(WorkflowActionInvocation(action_id="child.action"),),
                terminal=True,
                metadata={
                    "reads_context_keys": list(required_inputs),
                    "writes_context_keys": list(produced_outputs),
                },
            ),
        },
        termination_states=("child_start",),
        purpose="child",
    )


def _parent_with_subworkflow_contract(
    *,
    child_workflow_id: str = "#V#child_workflow",
    input_mappings: list[dict[str, str]] | None = None,
    output_mappings: list[dict[str, str]] | None = None,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="#V#parent_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID),),
                terminal=True,
                metadata={
                    "subworkflow_contract": build_subworkflow_contract(
                        workflow_id=child_workflow_id,
                        input_mappings=input_mappings
                        or [
                            {
                                "child_input_key": "child_input",
                                "parent_context_key": "parent_input",
                            }
                        ],
                        output_mappings=output_mappings
                        or [
                            {
                                "child_output_field": "child_output",
                                "parent_context_key": "parent_output",
                            }
                        ],
                    )
                },
            ),
        },
        termination_states=("start",),
        purpose="parent",
    )


def test_validate_contract_accepts_compatible_subworkflow_contract() -> None:
    child = _child_workflow_definition()
    parent = _parent_with_subworkflow_contract()

    validation = validate_workflow_definition_contract(
        definition=parent,
        known_workflow_ids={child.workflow_id},
        workflow_definition_loader=lambda workflow_id: (
            child if workflow_id == child.workflow_id else None
        ),
    )

    assert validation["valid"] is True
    assert validation["subworkflow_contract_issues"] == []
    assert "workflow_subworkflow_unresolved" not in (validation.get("errors") or [])


def test_validate_contract_reports_unresolved_subworkflow_workflow() -> None:
    parent = _parent_with_subworkflow_contract(child_workflow_id="#V#missing_child")

    validation = validate_workflow_definition_contract(
        definition=parent,
        known_workflow_ids={"#V#known_child"},
        workflow_definition_loader=lambda _workflow_id: None,
    )

    assert validation["valid"] is False
    assert "workflow_subworkflow_unresolved" in (validation.get("errors") or [])
    assert any(
        issue.get("reason_code") == "subworkflow_workflow_not_found"
        for issue in validation["subworkflow_contract_issues"]
    )


def test_validate_contract_reports_subworkflow_input_contract_mismatch() -> None:
    child = _child_workflow_definition(required_inputs=("required_child_input",))
    parent = _parent_with_subworkflow_contract(
        input_mappings=[
            {
                "child_input_key": "other_input",
                "parent_context_key": "parent_input",
            }
        ]
    )

    validation = validate_workflow_definition_contract(
        definition=parent,
        workflow_definition_loader=lambda workflow_id: (
            child if workflow_id == child.workflow_id else None
        ),
    )

    assert validation["valid"] is False
    assert "workflow_subworkflow_contract_mismatch" in (validation.get("errors") or [])
    assert any(
        issue.get("reason_code") == "subworkflow_input_contract_mismatch"
        for issue in validation["subworkflow_contract_issues"]
    )


def test_validate_contract_reports_subworkflow_output_contract_mismatch() -> None:
    child = _child_workflow_definition(produced_outputs=("known_child_output",))
    parent = _parent_with_subworkflow_contract(
        output_mappings=[
            {
                "child_output_field": "unknown_output",
                "parent_context_key": "parent_output",
            }
        ]
    )

    validation = validate_workflow_definition_contract(
        definition=parent,
        workflow_definition_loader=lambda workflow_id: (
            child if workflow_id == child.workflow_id else None
        ),
    )

    assert validation["valid"] is False
    assert "workflow_subworkflow_contract_mismatch" in (validation.get("errors") or [])
    assert any(
        issue.get("reason_code") == "subworkflow_output_contract_mismatch"
        for issue in validation["subworkflow_contract_issues"]
    )


def test_validate_contract_rejects_recursive_subworkflow_reference() -> None:
    parent = _parent_with_subworkflow_contract(child_workflow_id="#V#parent_workflow")

    validation = validate_workflow_definition_contract(definition=parent)

    assert validation["valid"] is False
    assert any(
        issue.get("reason_code") == "subworkflow_recursive_self_reference"
        for issue in validation["subworkflow_contract_issues"]
    )
