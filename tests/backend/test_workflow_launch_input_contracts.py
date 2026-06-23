from __future__ import annotations

from src.backend.workflows.workflow_launch_input_contracts import (
    WORKFLOW_LAUNCH_INPUT_EXTRACTOR_ARXIV_ID,
    WORKFLOW_LAUNCH_INPUT_EXTRACTOR_ARXIV_ID_LIST,
    WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION,
    WORKFLOW_REQUIRED_ACTOR_CONTEXT_MISSING,
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


def test_resolve_workflow_launch_inputs_can_read_selector_workflow_inputs() -> None:
    resolution = resolve_workflow_launch_inputs(
        workflow_id="#V#general_mail_review_workflow",
        contract={
            "schema_version": WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION,
            "required_inputs": ["prompt"],
            "input_mappings": [
                {
                    "target_context_key": "prompt",
                    "source_expression": "inputs.prompt",
                    "extractor": "identity",
                    "required": True,
                },
                {
                    "target_context_key": "mail_review_profile_id",
                    "source_expression": "inputs.selected_workflow_trace.selector_selection_metadata.workflow_inputs.mail_profile_id",
                    "extractor": "identity",
                    "required": False,
                },
                {
                    "target_context_key": "mail_review_output_fields",
                    "source_expression": "inputs.selected_workflow_trace.selector_selection_metadata.workflow_inputs.output_fields",
                    "extractor": "identity",
                    "required": False,
                },
            ],
        },
        inputs={
            "prompt": "Show my latest mail in a table.",
            "selected_workflow_trace": {
                "selector_selection_metadata": {
                    "workflow_inputs": {
                        "mail_profile_id": "vonwitbrock-gmail",
                        "output_fields": ["sender", "date", "subject", "labels"],
                    }
                }
            },
        },
    )

    assert dict(resolution.resolved_inputs) == {
        "prompt": "Show my latest mail in a table.",
        "mail_review_profile_id": "vonwitbrock-gmail",
        "mail_review_output_fields": ["sender", "date", "subject", "labels"],
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


def test_resolve_workflow_launch_inputs_falls_back_to_grounded_arxiv_contract() -> None:
    resolution = resolve_workflow_launch_inputs(
        workflow_id="#V#arxiv_paper_representation_workflow",
        contract={
            "schema_version": WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION,
            "required_inputs": ["prompt"],
            "input_mappings": [
                {
                    "target_context_key": "prompt",
                    "source_expression": "inputs.prompt",
                    "extractor": "identity",
                    "required": True,
                },
                {
                    "target_context_key": "arxiv_id",
                    "source_expression": "inputs.arxiv_id",
                    "extractor": "identity",
                    "required": False,
                },
                {
                    "target_context_key": "arxiv_id",
                    "source_expression": "inputs.turn_expected_outcome_contract.summary",
                    "extractor": WORKFLOW_LAUNCH_INPUT_EXTRACTOR_ARXIV_ID,
                    "required": False,
                },
                {
                    "target_context_key": "arxiv_id",
                    "source_expression": "inputs.workflow_discovery_result.discovery_query_input",
                    "extractor": WORKFLOW_LAUNCH_INPUT_EXTRACTOR_ARXIV_ID,
                    "required": False,
                },
            ],
        },
        inputs={
            "prompt": "represent the first one",
            "turn_expected_outcome_contract": {
                "summary": (
                    "Represent the first arXiv paper from the previous list, "
                    "i.e. arXiv:2604.04604."
                )
            },
            "workflow_discovery_result": {
                "discovery_query_input": (
                    "represent the first one\n\nSuccess target: arXiv:2604.04604"
                )
            },
        },
    )

    assert dict(resolution.resolved_inputs) == {
        "prompt": "represent the first one",
        "arxiv_id": "2604.04604",
    }
    assert resolution.unresolved_required_inputs == ()
    assert resolution.diagnostics.get("status") == "resolved"


def test_resolve_workflow_launch_inputs_extracts_all_grounded_arxiv_ids() -> None:
    resolution = resolve_workflow_launch_inputs(
        workflow_id="#V#arxiv_paper_representation_workflow",
        contract={
            "schema_version": WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION,
            "required_inputs": ["prompt"],
            "input_mappings": [
                {
                    "target_context_key": "prompt",
                    "source_expression": "inputs.prompt",
                    "extractor": "identity",
                    "required": True,
                },
                {
                    "target_context_key": "arxiv_ids",
                    "source_expression": "inputs.prompt",
                    "extractor": WORKFLOW_LAUNCH_INPUT_EXTRACTOR_ARXIV_ID_LIST,
                    "required": False,
                },
            ],
        },
        inputs={
            "prompt": (
                "Represent both papers:\n"
                "https://arxiv.org/abs/2406.15341\n"
                "https://arxiv.org/abs/2507.21035"
            )
        },
    )

    assert dict(resolution.resolved_inputs) == {
        "prompt": (
            "Represent both papers:\n"
            "https://arxiv.org/abs/2406.15341\n"
            "https://arxiv.org/abs/2507.21035"
        ),
        "arxiv_ids": ["2406.15341", "2507.21035"],
    }
    assert resolution.unresolved_required_inputs == ()
    assert resolution.diagnostics.get("status") == "resolved"


def test_resolve_workflow_launch_inputs_binds_authenticated_actor_context() -> None:
    resolution = resolve_workflow_launch_inputs(
        workflow_id="#V#generic_actor_context_workflow",
        contract={
            "schema_version": WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION,
            "required_inputs": ["actor_concept_id"],
            "input_mappings": [
                {
                    "target_context_key": "actor_concept_id",
                    "source_expression": "inputs.actor_concept_id",
                    "extractor": "identity",
                    "required": False,
                },
                {
                    "target_context_key": "actor_concept_id",
                    "source_expression": "inputs.user_concept_id",
                    "extractor": "identity",
                    "required": True,
                },
                {
                    "target_context_key": "organisation_concept_id",
                    "source_expression": "inputs.org_concept_id",
                    "extractor": "identity",
                    "required": False,
                },
                {
                    "target_context_key": "namespace",
                    "source_expression": "inputs.user_namespace",
                    "extractor": "identity",
                    "required": False,
                },
            ],
        },
        inputs={
            "user_concept_id": "#V#michael_witbrock",
            "org_concept_id": "#V#university_of_auckland_strong_ai_lab",
            "user_namespace": (
                "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
            ),
        },
        contract_source="test_contract",
    )

    assert dict(resolution.resolved_inputs) == {
        "actor_concept_id": "#V#michael_witbrock",
        "organisation_concept_id": "#V#university_of_auckland_strong_ai_lab",
        "namespace": "#V#michael_witbrock@university_of_auckland_strong_ai_lab",
    }
    assert resolution.unresolved_required_inputs == ()
    assert resolution.diagnostics.get("status") == "resolved"
    assert resolution.diagnostics.get("actor_context_binding") == {
        "schema_version": "workflow_actor_context_binding.v1",
        "required_fields": ["actor_concept_id"],
        "bound_fields": [
            "actor_concept_id",
            "namespace",
            "organisation_concept_id",
        ],
        "missing_fields": [],
        "status": "bound",
    }


def test_resolve_workflow_launch_inputs_reports_typed_missing_actor_context() -> None:
    resolution = resolve_workflow_launch_inputs(
        workflow_id="#V#generic_actor_context_workflow",
        contract={
            "schema_version": WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION,
            "required_inputs": ["actor_concept_id"],
            "input_mappings": [
                {
                    "target_context_key": "actor_concept_id",
                    "source_expression": "inputs.user_concept_id",
                    "extractor": "identity",
                    "required": True,
                }
            ],
        },
        inputs={"prompt": "Run the authenticated workflow."},
    )

    assert dict(resolution.resolved_inputs) == {}
    assert resolution.unresolved_required_inputs == ("actor_concept_id",)
    assert resolution.diagnostics.get("status") == "failed"
    assert (
        resolution.diagnostics.get("failure_code")
        == WORKFLOW_REQUIRED_ACTOR_CONTEXT_MISSING
    )
    assert resolution.diagnostics.get("actor_context_binding") == {
        "schema_version": "workflow_actor_context_binding.v1",
        "required_fields": ["actor_concept_id"],
        "bound_fields": [],
        "missing_fields": ["actor_concept_id"],
        "status": "failed",
    }


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
