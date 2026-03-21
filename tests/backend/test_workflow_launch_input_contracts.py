from __future__ import annotations

from src.backend.workflows.workflow_launch_input_contracts import (
    WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION,
    normalise_workflow_launch_input_contract,
    resolve_workflow_launch_inputs,
)


def test_resolve_workflow_launch_inputs_extracts_declared_values() -> None:
    resolution = resolve_workflow_launch_inputs(
        workflow_id="#V#meeting_invitation_testing_workflow",
        contract={
            "schema_version": WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION,
            "required_inputs": ["invitation_text"],
            "input_mappings": [
                {
                    "target_context_key": "invitation_text",
                    "source_expression": "inputs.prompt",
                    "extractor": "first_quoted_text",
                    "required": True,
                },
                {
                    "target_context_key": "candidate_workflow_ids",
                    "source_expression": "inputs.workflow_discovery_result.matches",
                    "extractor": "workflow_id_list",
                },
            ],
        },
        inputs={
            "prompt": (
                'Run a meeting-invitation test on this invitation text:\n\n'
                '"Kia ora team, please join us on Tuesday at 2:00pm in Room 4."'
            ),
            "workflow_discovery_result": {
                "matches": [
                    {"concept_id": "#V#meeting_invitation_testing_workflow"},
                    {"workflow_id": "#V#synthetic_workflow_regression_suite_workflow"},
                ]
            },
        },
        contract_source="text_relation:#V#hasWorkflowLaunchInputContractJson",
    )

    assert dict(resolution.resolved_inputs) == {
        "invitation_text": "Kia ora team, please join us on Tuesday at 2:00pm in Room 4.",
        "candidate_workflow_ids": [
            "#V#meeting_invitation_testing_workflow",
            "#V#synthetic_workflow_regression_suite_workflow",
        ],
    }
    assert resolution.unresolved_required_inputs == ()
    assert resolution.diagnostics.get("status") == "resolved"


def test_resolve_workflow_launch_inputs_fails_closed_for_missing_required_value() -> None:
    resolution = resolve_workflow_launch_inputs(
        workflow_id="#V#meeting_invitation_testing_workflow",
        contract={
            "schema_version": WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION,
            "required_inputs": ["invitation_text"],
            "input_mappings": [
                {
                    "target_context_key": "invitation_text",
                    "source_expression": "inputs.prompt",
                    "extractor": "first_quoted_text",
                    "required": True,
                }
            ],
        },
        inputs={"prompt": "Run the test without a quoted invitation specimen."},
    )

    assert dict(resolution.resolved_inputs) == {}
    assert resolution.unresolved_required_inputs == ("invitation_text",)
    assert resolution.diagnostics.get("status") == "failed"
    assert resolution.diagnostics.get("unresolved_required_inputs") == [
        "invitation_text"
    ]


def test_normalise_workflow_launch_input_contract_rejects_invalid_extractor() -> None:
    contract, error = normalise_workflow_launch_input_contract(
        {
            "schema_version": WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION,
            "input_mappings": [
                {
                    "target_context_key": "invitation_text",
                    "source_expression": "inputs.prompt",
                    "extractor": "unsupported_extractor",
                }
            ],
        }
    )

    assert contract is None
    assert error == "workflow_launch_input_mapping_extractor_invalid"
