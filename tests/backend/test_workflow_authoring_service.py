from __future__ import annotations

import pytest

from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from src.backend.workflows import workflow_concept_authority_service as authority_mod
from src.backend.workflows.workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
    strip_transient_execution_defaults_from_authoring_spec,
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
                        inputs={
                            "query": {"$context_key": "user_query"},
                            "top_k": 5,
                        },
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
    assert "inputs" not in authoring_spec["steps"][0]
    assert authoring_spec["steps"][0]["static_input_bindings"] == [
        {"tool_param": "top_k", "value": 5}
    ]
    assert authoring_spec["steps"][0]["context_input_mappings"] == [
        {"tool_param": "query", "context_key": "user_query"}
    ]
    assert rebuilt.purpose == "Demo workflow description"
    assert rebuilt.states["start"].actions[0].inputs == {
        "query": {"$context_key": "user_query"},
        "top_k": 5,
    }
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


def test_build_workflow_definition_from_authoring_spec_accepts_explicit_input_schemas():
    definition = build_workflow_definition_from_authoring_spec(
        {
            "workflow_id": "#V#explicit_inputs_workflow",
            "initial_state_key": "start",
            "steps": [
                {
                    "state_id": "start",
                    "action_id": "demo.action",
                    "static_input_bindings": [
                        {"tool_param": "limit", "value": 5},
                    ],
                    "context_input_mappings": [
                        {
                            "tool_param": "query",
                            "context_key": "user_query",
                            "required": True,
                            "mapping_concept_id": "#V#mapping_query",
                        }
                    ],
                    "next_state": "done",
                },
                {"state_id": "done", "terminal": True},
            ],
        }
    )

    assert definition.states["start"].actions[0].inputs == {
        "limit": 5,
        "query": {
            "$context_key": "user_query",
            "$mapping_concept_id": "#V#mapping_query",
            "$required": True,
        },
    }


def test_strip_transient_execution_defaults_from_authoring_spec_removes_persisted_turn_defaults():
    cleaned = strip_transient_execution_defaults_from_authoring_spec(
        {
            "workflow_id": "#V#candidate_workflow",
            "steps": [
                {
                    "state_id": "start",
                    "inputs": {
                        "default_request_text": "Recover this workflow.",
                        "query": "stable",
                    },
                    "static_input_bindings": [
                        {
                            "tool_param": "workflow_gap_base_response_text",
                            "value": "Fallback reply.",
                        },
                        {"tool_param": "limit", "value": 5},
                    ],
                    "context_input_mappings": [
                        {
                            "tool_param": "workflow_gap_request_text",
                            "context_key": "workflow_gap_request_text",
                        }
                    ],
                }
            ],
        }
    )

    step = cleaned["steps"][0]
    assert step["inputs"] == {"query": "stable"}
    assert step["static_input_bindings"] == [{"tool_param": "limit", "value": 5}]
    assert step["context_input_mappings"] == [
        {
            "tool_param": "workflow_gap_request_text",
            "context_key": "workflow_gap_request_text",
        }
    ]


def test_authoring_roundtrip_preserves_explicit_step_concept_ids():
    definition = WorkflowDefinition(
        workflow_id="#V#concept_search_instance_retrieval_workflow",
        initial_state="execute",
        states={
            "execute": WorkflowStateSpec(
                state_id="execute",
                actions=(
                    WorkflowActionInvocation(
                        action_id="workflow_gap.execute_candidate",
                        inputs={"prompt_concept_id": "#V#workflow_gap_candidate_execution_prompt"},
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="completed",
                        condition=lambda _ctx: True,
                        condition_spec={"kind": "always"},
                        reason="next_step",
                    ),
                ),
                metadata={
                    "workflow_step_concept_id": "#V#workflow_step_concept_search_instance_retrieval_workflow_execute_candidate"
                },
            ),
            "completed": WorkflowStateSpec(
                state_id="completed",
                terminal=True,
                metadata={
                    "workflow_step_concept_id": "#V#workflow_step_concept_search_instance_retrieval_workflow_completed"
                },
            ),
        },
        termination_states=("completed",),
        purpose="Existing workflow repair",
    )

    authoring_spec = serialise_workflow_definition_to_authoring_spec(definition)
    rebuilt = build_workflow_definition_from_authoring_spec(authoring_spec)
    concept_ids = authority_mod.publication_spec_step_concept_ids(
        workflow_id=rebuilt.workflow_id,
        spec=authority_mod._build_publication_spec_from_definition(rebuilt),
    )

    assert authoring_spec["steps"][0]["concept_id"] == (
        "#V#workflow_step_concept_search_instance_retrieval_workflow_execute_candidate"
    )
    assert rebuilt.states["execute"].metadata["workflow_step_concept_id"] == (
        "#V#workflow_step_concept_search_instance_retrieval_workflow_execute_candidate"
    )
    assert concept_ids[0] == (
        "#V#workflow_step_concept_search_instance_retrieval_workflow_execute_candidate"
    )
