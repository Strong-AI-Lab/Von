from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from src.backend.workflows.durable.workflow_instance_submission_service import (
    invalidate_workflow_runnable_verification_cache,
    verify_workflow_runnable,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
)
from src.backend.workflows.subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    build_subworkflow_contract,
)


@pytest.fixture(autouse=True)
def _clear_runnable_cache_between_tests():
    invalidate_workflow_runnable_verification_cache(reason="test_fixture_pre")
    yield
    invalidate_workflow_runnable_verification_cache(reason="test_fixture_post")


def _make_definition(*, include_action: bool) -> WorkflowDefinition:
    states = {
        "#V#start": WorkflowStateSpec(
            state_id="#V#start",
            actions=(
                [WorkflowActionInvocation(action_id="tool.initial")]
                if include_action
                else ()
            ),
            terminal=True,
        )
    }
    return WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states=states,
        termination_states=("#V#start",),
    )


def _make_registry(
    definition: WorkflowDefinition | None,
    *,
    workflow_id: str = "#V#candidate_workflow",
    extra_definitions: dict[str, WorkflowDefinition] | None = None,
) -> MagicMock:
    registry = MagicMock()
    definitions = {
        key: value
        for key, value in (
            {
                workflow_id: definition,
                **(extra_definitions or {}),
            }
        ).items()
        if value is not None
    }
    registry.get.side_effect = lambda item: definitions.get(item)
    registry.all_workflow_ids.return_value = list(definitions.keys())
    registration = MagicMock()
    registration.source = "vontology"
    registry.get_registration.return_value = registration
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
    assert verification.contract_validation is not None
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
    assert verification.definition_identity is not None
    assert verification.contract_validation is not None
    assert verification.contract_validation.get("valid") is True


def test_verify_workflow_runnable_rejects_missing_transition_from_action_state() -> None:
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
    definition = WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states={
            "#V#start": WorkflowStateSpec(
                state_id="#V#start",
                actions=(WorkflowActionInvocation(action_id="tool.initial"),),
                terminal=False,
            )
        },
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
    assert "workflow_control_flow_incomplete" in verification.errors


def test_verify_workflow_runnable_accepts_shared_conversation_actions_via_fallback() -> None:
    workflow_action_ids = (
        "shared_conversation_create_session",
        "shared_conversation_join_session",
        "shared_conversation_invite_create",
        "shared_conversation_list_invites",
        "shared_conversation_respond_invite",
    )
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_action": workflow_action_ids[0],
            }
        ],
        "edges": [],
        "warnings": [],
    }
    definition = WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states={
            "#V#start": WorkflowStateSpec(
                state_id="#V#start",
                actions=tuple(
                    WorkflowActionInvocation(action_id=action_id)
                    for action_id in workflow_action_ids
                ),
                terminal=True,
            )
        },
        termination_states=("#V#start",),
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph",
        return_value=(graph, []),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_registry_read_only",
        return_value=_make_registry(definition),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_durable_action_registry",
        return_value=_make_action_registry(supports_action=False, fallback=True),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service._internal_mcp_method_names",
        return_value=frozenset(workflow_action_ids),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is True
    assert set(verification.discovered_action_ids) == set(workflow_action_ids)
    assert verification.unsupported_action_ids == ()
    assert verification.contract_validation is not None
    assert verification.contract_validation.get("valid") is True


def test_verify_workflow_runnable_caches_for_identical_definition() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {"step_id": "#V#start", "name": "Start", "invokes_action": "tool.initial"}
        ],
        "edges": [],
        "warnings": [],
    }
    registry = _make_registry(_make_definition(include_action=True))
    action_registry = _make_action_registry(supports_action=True)

    invalidate_workflow_runnable_verification_cache(
        reason="test_setup",
        workflow_id="#V#candidate_workflow",
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph",
        return_value=(graph, []),
    ) as mock_graph, patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_registry_read_only",
        return_value=registry,
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_durable_action_registry",
        return_value=action_registry,
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service._internal_mcp_method_names",
        return_value=frozenset(),
    ):
        first = verify_workflow_runnable("#V#candidate_workflow")
        second = verify_workflow_runnable("#V#candidate_workflow")

    assert first.runnable_verification_success is True
    assert second.runnable_verification_success is True
    assert mock_graph.call_count == 1
    assert (first.verification_telemetry or {}).get("cache_hit") is False
    assert (second.verification_telemetry or {}).get("cache_hit") is True


def test_verify_workflow_runnable_cache_invalidation_forces_recompute() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {"step_id": "#V#start", "name": "Start", "invokes_action": "tool.initial"}
        ],
        "edges": [],
        "warnings": [],
    }
    registry = _make_registry(_make_definition(include_action=True))
    action_registry = _make_action_registry(supports_action=True)

    invalidate_workflow_runnable_verification_cache(
        reason="test_setup",
        workflow_id="#V#candidate_workflow",
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph",
        return_value=(graph, []),
    ) as mock_graph, patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_registry_read_only",
        return_value=registry,
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_durable_action_registry",
        return_value=action_registry,
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service._internal_mcp_method_names",
        return_value=frozenset(),
    ):
        verify_workflow_runnable("#V#candidate_workflow")
        invalidate_workflow_runnable_verification_cache(
            reason="test_invalidation",
            workflow_id="#V#candidate_workflow",
        )
        second = verify_workflow_runnable("#V#candidate_workflow")

    assert mock_graph.call_count == 2
    assert (second.verification_telemetry or {}).get("cache_hit") is False


def test_verify_workflow_runnable_fail_closed_on_registry_exception() -> None:
    invalidate_workflow_runnable_verification_cache(
        reason="test_setup",
        workflow_id="#V#candidate_workflow",
    )
    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_registry_read_only",
        side_effect=RuntimeError("registry unavailable"),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is False
    assert "workflow_runnable_check_failed" in verification.errors


def test_verify_workflow_runnable_reports_unresolved_subworkflow_contract() -> None:
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_workflow": "#V#missing_child",
            }
        ],
        "edges": [],
        "warnings": [],
    }
    parent_definition = WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states={
            "#V#start": WorkflowStateSpec(
                state_id="#V#start",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                    ),
                ),
                terminal=True,
                metadata={
                    "subworkflow_contract": build_subworkflow_contract(
                        workflow_id="#V#missing_child",
                        input_mappings=[
                            {
                                "child_input_key": "child_input",
                                "parent_context_key": "parent_input",
                            }
                        ],
                        output_mappings=[
                            {
                                "child_output_field": "child_output",
                                "parent_context_key": "parent_output",
                            }
                        ],
                    )
                },
            )
        },
        termination_states=("#V#start",),
    )

    with patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_process_graph",
        return_value=(graph, []),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_workflow_registry_read_only",
        return_value=_make_registry(parent_definition),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_durable_action_registry",
        return_value=_make_action_registry(supports_action=True),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.load_workflow_definition_from_vontology",
        return_value=None,
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is False
    assert "workflow_subworkflow_unresolved" in verification.errors
    contract = verification.contract_validation or {}
    assert any(
        issue.get("reason_code") == "subworkflow_workflow_not_found"
        for issue in contract.get("subworkflow_contract_issues", [])
    )


def test_verify_workflow_runnable_accepts_resolved_subworkflow_contract() -> None:
    child_definition = WorkflowDefinition(
        workflow_id="#V#child_workflow",
        initial_state="#V#child_start",
        states={
            "#V#child_start": WorkflowStateSpec(
                state_id="#V#child_start",
                terminal=True,
                metadata={
                    "reads_context_keys": ["child_input"],
                    "writes_context_keys": ["child_output"],
                },
            )
        },
        termination_states=("#V#child_start",),
    )
    parent_definition = WorkflowDefinition(
        workflow_id="#V#candidate_workflow",
        initial_state="#V#start",
        states={
            "#V#start": WorkflowStateSpec(
                state_id="#V#start",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                    ),
                ),
                terminal=True,
                metadata={
                    "subworkflow_contract": build_subworkflow_contract(
                        workflow_id="#V#child_workflow",
                        input_mappings=[
                            {
                                "child_input_key": "child_input",
                                "parent_context_key": "parent_input",
                            }
                        ],
                        output_mappings=[
                            {
                                "child_output_field": "child_output",
                                "parent_context_key": "parent_output",
                            }
                        ],
                    )
                },
            )
        },
        termination_states=("#V#start",),
    )
    definitions = {
        "#V#candidate_workflow": parent_definition,
        "#V#child_workflow": child_definition,
    }
    graph = {
        "workflow_id": "#V#candidate_workflow",
        "initial_step": "#V#start",
        "steps": [
            {
                "step_id": "#V#start",
                "name": "Start",
                "invokes_workflow": "#V#child_workflow",
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
        return_value=_make_registry(
            parent_definition,
            extra_definitions={"#V#child_workflow": child_definition},
        ),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.build_durable_action_registry",
        return_value=_make_action_registry(supports_action=True),
    ), patch(
        "src.backend.workflows.durable.workflow_instance_submission_service.load_workflow_definition_from_vontology",
        side_effect=lambda workflow_id: definitions.get(workflow_id),
    ):
        verification = verify_workflow_runnable("#V#candidate_workflow")

    assert verification.runnable_verification_success is True
    assert "workflow_subworkflow_unresolved" not in verification.errors
    assert "workflow_subworkflow_contract_mismatch" not in verification.errors
