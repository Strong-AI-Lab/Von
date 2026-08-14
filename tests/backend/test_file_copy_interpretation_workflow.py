from __future__ import annotations

from src.backend.services.workflow_turn_capability_service import (
    _workflow_model_input_schema,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.file_copy_interpretation_workflow import (
    FILE_COPY_INTERPRETATION_LAUNCH_INPUT_CONTRACT,
    FILE_COPY_INTERPRETATION_WORKFLOW_ID,
    build_file_copy_interpretation_workflow_test_definition,
    build_file_copy_interpretation_workflow_test_registration,
    register_file_copy_interpretation_actions,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.workflow_concept_authority_service import (
    build_repo_seed_workflow_definitions,
)
from src.backend.workflows.workflow_launch_input_contracts import (
    resolve_workflow_launch_inputs,
)


def test_build_file_copy_interpretation_workflow_test_definition_definition_shape() -> None:
    workflow = build_file_copy_interpretation_workflow_test_definition()

    assert workflow.workflow_id == FILE_COPY_INTERPRETATION_WORKFLOW_ID
    assert workflow.initial_state == "interpret"
    assert set(workflow.states.keys()) == {"interpret", "index", "complete", "failed"}
    assert workflow.termination_states == ("complete", "failed")
    assert workflow.metadata["launch_input_contract"] == (
        FILE_COPY_INTERPRETATION_LAUNCH_INPUT_CONTRACT
    )

    interpret_actions = [action.action_id for action in workflow.states["interpret"].actions]
    assert interpret_actions == ["interpret_file_copy"]
    interpret_inputs = dict(workflow.states["interpret"].actions[0].inputs)
    assert set(interpret_inputs) == {"concept_id"}
    assert interpret_inputs["concept_id"]["$context_key"] == "file_copy_concept_id"
    assert (
        interpret_inputs["concept_id"]["$mapping_concept_id"]
        == "#V#workflow_mapping_file_copy_interpretation_workflow_interpret_"
        "file_copy_concept_id_to_concept_id_parameter"
    )
    index_actions = [action.action_id for action in workflow.states["index"].actions]
    assert index_actions == ["index_file_copy"]
    index_inputs = dict(workflow.states["index"].actions[0].inputs)
    assert set(index_inputs) == {"concept_id"}
    assert index_inputs["concept_id"]["$context_key"] == "file_copy_concept_id"
    assert (
        index_inputs["concept_id"]["$mapping_concept_id"]
        == "#V#workflow_mapping_file_copy_interpretation_workflow_index_"
        "file_copy_concept_id_to_concept_id_parameter"
    )


def test_build_file_copy_interpretation_workflow_test_registration() -> None:
    registration = build_file_copy_interpretation_workflow_test_registration()

    assert registration.workflow_id == FILE_COPY_INTERPRETATION_WORKFLOW_ID
    assert registration.source == "built_in"
    assert registration.definition.workflow_id == FILE_COPY_INTERPRETATION_WORKFLOW_ID


def test_repo_seed_launch_contract_exposes_only_file_copy_identifier() -> None:
    workflow = build_repo_seed_workflow_definitions(
        target_workflow_ids=[FILE_COPY_INTERPRETATION_WORKFLOW_ID]
    )[FILE_COPY_INTERPRETATION_WORKFLOW_ID]
    launch_contract = workflow.metadata.get("launch_input_contract")

    assert launch_contract == FILE_COPY_INTERPRETATION_LAUNCH_INPUT_CONTRACT
    assert workflow.metadata.get("launch_input_contract_source") == (
        "repo_seed_bundle:launch_input_contract"
    )

    file_copy_id = "#V#computer_file_copy_launch_contract_regression"
    resolution = resolve_workflow_launch_inputs(
        workflow_id=FILE_COPY_INTERPRETATION_WORKFLOW_ID,
        contract=launch_contract,
        inputs={"file_copy_concept_id": file_copy_id},
        contract_source=workflow.metadata.get("launch_input_contract_source"),
    )

    assert resolution.unresolved_required_inputs == ()
    assert resolution.resolved_inputs == {"file_copy_concept_id": file_copy_id}

    model_schema = _workflow_model_input_schema(launch_contract)
    inputs_schema = model_schema["properties"]["inputs"]
    assert list(inputs_schema["properties"]) == ["file_copy_concept_id"]
    assert inputs_schema["required"] == ["file_copy_concept_id"]
    assert [
        mapping["target_context_key"]
        for mapping in model_schema["x-von-represented-launch-input-mappings"]
    ] == ["file_copy_concept_id"]


def test_repo_seed_single_file_copy_input_passes_metadata_validation() -> None:
    workflow = build_repo_seed_workflow_definitions(
        target_workflow_ids=[FILE_COPY_INTERPRETATION_WORKFLOW_ID]
    )[FILE_COPY_INTERPRETATION_WORKFLOW_ID]
    file_copy_id = "#V#computer_file_copy_metadata_regression"
    conflicting_legacy_id = "#V#computer_file_copy_wrong_legacy_alias"
    resolution = resolve_workflow_launch_inputs(
        workflow_id=FILE_COPY_INTERPRETATION_WORKFLOW_ID,
        contract=workflow.metadata.get("launch_input_contract"),
        inputs={
            "file_copy_concept_id": file_copy_id,
            "concept_id": conflicting_legacy_id,
        },
        contract_source=workflow.metadata.get("launch_input_contract_source"),
    )
    execution_data = {
        "file_copy_concept_id": file_copy_id,
        "concept_id": conflicting_legacy_id,
    }
    execution_data.update(resolution.resolved_inputs)

    for state_id in ("interpret", "index"):
        state = workflow.states[state_id]
        assert state.metadata["reads_context_keys"] == ["file_copy_concept_id"]
        assert set(state.actions[0].inputs) == {"concept_id"}
        assert state.actions[0].inputs["concept_id"]["$context_key"] == (
            "file_copy_concept_id"
        )

    observed_inputs: list[dict[str, object]] = []

    def record_inputs(request: WorkflowActionRequest) -> WorkflowActionResult:
        observed_inputs.append(dict(request.inputs))
        return WorkflowActionResult(status="success", outputs={})

    registry = ActionRegistry()
    registry.register(
        ActionSpec(action_id="interpret_file_copy", handler=record_inputs)
    )
    registry.register(ActionSpec(action_id="index_file_copy", handler=record_inputs))

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        workflow,
        environment=WorkflowEnvironment(llm_client=None),
        data=execution_data,
    )

    assert result.completed is True
    assert result.final_state == "complete"
    assert result.error is None
    assert len(observed_inputs) == 2
    assert observed_inputs == [
        {"concept_id": file_copy_id},
        {"concept_id": file_copy_id},
    ]
    validation_events = result.data.get("workflow_metadata_validation_events", [])
    assert len(validation_events) == 2
    assert all(event.get("phase") == "pre_action" for event in validation_events)
    assert all(event.get("ok") is True for event in validation_events)


def test_register_file_copy_interpretation_actions_is_noop() -> None:
    registry = ActionRegistry()
    register_file_copy_interpretation_actions(registry)
    assert registry.all_action_ids() == []
