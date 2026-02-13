from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.backend.workflows.durable.workflow_instance_submission_service import (
    verify_workflow_runnable,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
)


def _make_definition(*, include_action: bool) -> WorkflowDefinition:
    states = {
        "#V#start": WorkflowStateSpec(
            state_id="#V#start",
            actions=(
                [WorkflowActionInvocation(action_id="tool.initial")]
                if include_action
                else ()
            ),
        )
    }
    return WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states=states,
    )


def _make_registry(definition: WorkflowDefinition | None) -> MagicMock:
    registry = MagicMock()
    registry.get.return_value = definition
    return registry


def _make_action_registry(*, supports_action: bool, fallback: bool = False) -> MagicMock:
    registry = MagicMock()
    registry.has_fallback_handler.return_value = fallback
    if supports_action:
        registry.has.return_value = True
    else:
        registry.has.return_value = False
    return registry


def test_verify_workflow_runnable_rejects_initial_vacuous_step() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": None,
                "preconditions": [],
                "effects": [],
                "reads_variables": [],
                "writes_variables": [],
            }
        ],
        "edges": [],
        "warnings": [],
    }

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph",
        return_value=(graph, []),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_registry_read_only",
        return_value=_make_registry(_make_definition(include_action=False)),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_durable_action_registry",
        return_value=_make_action_registry(supports_action=True),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is False
    assert verification.unsupported_action_ids == ()
    assert "workflow_step_contract_integrity_issue" in verification.errors
    assert len(verification.integrity_issues) == 1
    issue = verification.integrity_issues[0]
    assert issue["step_id"] == "#V#start"
    previous = issue["previous_steps"][0]
    assert previous["step_id"] == "__workflow_input__"
    assert previous["step_name"] == "Workflow input"


def test_verify_workflow_runnable_reports_previous_step_context_for_middle_vacuity() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": "tool.initial",
            },
            {
                "step_id": "#V#middle",
                "name": "Middle",
                "invokes_action": None,
            },
            {
                "step_id": "#V#end",
                "name": "End",
                "invokes_action": "tool.done",
            },
        ],
        "edges": [
            {"from": "#V#start", "to": "#V#middle", "predicate": "nextStep"},
            {"from": "#V#middle", "to": "#V#end", "predicate": "nextStep"},
        ],
        "warnings": [],
    }

    states = {
        "#V#start": WorkflowStateSpec(
            state_id="#V#start",
            actions=(WorkflowActionInvocation(action_id="tool.initial"),),
        ),
        "#V#middle": WorkflowStateSpec(
            state_id="#V#middle",
        ),
        "#V#end": WorkflowStateSpec(
            state_id="#V#end",
            actions=(WorkflowActionInvocation(action_id="tool.done"),),
        ),
    }
    definition = WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states=states,
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph",
        return_value=(graph, []),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_registry_read_only",
        return_value=_make_registry(definition),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_durable_action_registry",
        return_value=_make_action_registry(supports_action=True),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is False
    assert len(verification.integrity_issues) == 1
    issue = verification.integrity_issues[0]
    assert issue["step_id"] == "#V#middle"
    previous = issue["previous_steps"][0]
    assert previous["step_id"] == "#V#start"
    assert previous["invokes_action"] == "tool.initial"
    assert previous["link_predicate"] == "nextStep"


def test_verify_workflow_runnable_allows_workflows_with_contracts() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": "tool.initial",
            }
        ],
        "edges": [],
        "warnings": [],
    }

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph",
        return_value=(graph, []),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_registry_read_only",
        return_value=_make_registry(_make_definition(include_action=True)),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_durable_action_registry",
        return_value=_make_action_registry(supports_action=True),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is True
    assert verification.integrity_issues == ()
    assert verification.errors == ()
