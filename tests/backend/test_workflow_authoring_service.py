from __future__ import annotations

import pytest

from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from src.backend.workflows.workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
    serialise_workflow_definition_to_authoring_spec,
)


def test_authoring_roundtrip_preserves_workflow_metadata_and_description_alias():
    definition = WorkflowDefinition(
        workflow_id="#V#demo_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(
                    WorkflowActionInvocation(
                        action_id="search_concepts",
                        inputs={"query": "demo"},
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=lambda _ctx: True,
                        condition_spec={"kind": "always"},
                        reason="next_step",
                    ),
                ),
                metadata={
                    "writes_context_keys": ["result"],
                    "tool_output_context_mappings": [
                        {"tool_output_field": "result.text", "context_key": "result"}
                    ],
                },
            ),
            "done": WorkflowStateSpec(
                state_id="done",
                terminal=True,
                metadata={"completion_note": "Finished"},
            ),
        },
        termination_states=("done",),
        purpose="Demo workflow description",
        metadata={
            "required_effects": ["effect.ready"],
            "postcondition_probe": {"kind": "output_field", "field": "result"},
            "verification_inputs": {"request_id": "req_1"},
            "background_launch_policy": {"mode": "manual"},
            "plan_state_policy": {"mode": "append_only"},
        },
    )

    authoring_spec = serialise_workflow_definition_to_authoring_spec(definition)
    rebuilt = build_workflow_definition_from_authoring_spec(authoring_spec)

    assert authoring_spec["description"] == "Demo workflow description"
    assert authoring_spec["workflow_metadata"]["background_launch_policy"] == {
        "mode": "manual"
    }
    assert rebuilt.purpose == "Demo workflow description"
    assert rebuilt.metadata["required_effects"] == ["effect.ready"]
    assert rebuilt.metadata["postcondition_probe"] == {
        "kind": "output_field",
        "field": "result",
    }
    assert rebuilt.metadata["verification_inputs"] == {"request_id": "req_1"}
    assert rebuilt.metadata["background_launch_policy"] == {"mode": "manual"}
    assert rebuilt.metadata["plan_state_policy"] == {"mode": "append_only"}


def test_build_workflow_definition_from_authoring_spec_rejects_duplicate_state_ids():
    with pytest.raises(
        ValueError, match="workflow_authoring_spec_duplicate_state_id:start"
    ):
        build_workflow_definition_from_authoring_spec(
            {
                "workflow_id": "#V#demo_workflow",
                "description": "Demo workflow description",
                "initial_state_key": "start",
                "steps": [
                    {"state_id": "start", "next_state_key": "done"},
                    {"state_id": "start", "terminal": True},
                ],
            }
        )


def test_build_workflow_definition_from_authoring_spec_accepts_plain_transition_aliases():
    definition = build_workflow_definition_from_authoring_spec(
        {
            "workflow_id": "#V#alias_demo_workflow",
            "initial_state_key": "start",
            "steps": [
                {
                    "state_id": "start",
                    "action_id": "demo.action",
                    "next_state": "middle",
                    "on_failure_state": "failed",
                },
                {
                    "state_id": "middle",
                    "action_id": "demo.second_action",
                    "on_true_state": "done",
                    "on_false_state": "failed",
                },
                {"state_id": "done", "terminal": True},
                {"state_id": "failed", "terminal": True},
            ],
        }
    )

    start_state = definition.states["start"]
    middle_state = definition.states["middle"]

    assert start_state.terminal is False
    assert {transition.to_state for transition in start_state.transitions} == {
        "middle",
        "failed",
    }
    assert middle_state.terminal is False
    assert {transition.to_state for transition in middle_state.transitions} == {
        "done",
        "failed",
    }
