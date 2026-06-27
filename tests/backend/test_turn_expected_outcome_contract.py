from src.backend.workflows.turn_expected_outcome_contract import (
    TurnExpectedOutcomeContract,
    build_turn_expected_outcome_boundary_payload,
)


def test_turn_expected_outcome_contract_preserves_target_type_ids() -> None:
    contract = TurnExpectedOutcomeContract.from_mapping(
        {
            "expected_outcome_summary": "List grounded papers.",
            "required_tools": [
                "get_predicate_incidence",
                "find_relations_with_argument",
            ],
            "target_type_ids": [
                "#V#scholarly_article",
                "#V#scholarly_work",
                "#V#scholarly_article",
            ],
        }
    )

    assert contract.target_type_ids == (
        "#V#scholarly_article",
        "#V#scholarly_work",
    )
    assert contract.to_state_payload()["target_type_ids"] == [
        "#V#scholarly_article",
        "#V#scholarly_work",
    ]
    target_contracts = contract.to_state_payload()["target_contracts"]
    assert target_contracts == [
        {
            "schema_version": "turn_target_contract.v1",
            "kind": "symbolic",
            "binding_kind": "type",
            "resolution_status": "resolved",
            "matching_policy": "exact",
            "concept_ids": [
                "#V#scholarly_article",
                "#V#scholarly_work",
            ],
            "source": "target_type_ids",
        }
    ]


def test_turn_expected_outcome_boundary_profile_includes_target_type_ids() -> None:
    payload = build_turn_expected_outcome_boundary_payload(
        {
            "summary": "List grounded papers.",
            "target_type_ids": ["#V#scholarly_article"],
        }
    )

    assert payload["turn_expected_outcome_profile"]["target_type_ids"] == [
        "#V#scholarly_article"
    ]
    assert payload["turn_expected_outcome_contract_state"]["target_type_ids"] == [
        "#V#scholarly_article"
    ]
    assert payload["turn_expected_target_type_ids"] == ["#V#scholarly_article"]
    assert payload["turn_expected_target_contracts"] == [
        {
            "schema_version": "turn_target_contract.v1",
            "kind": "symbolic",
            "binding_kind": "type",
            "resolution_status": "resolved",
            "matching_policy": "exact",
            "concept_ids": ["#V#scholarly_article"],
            "source": "target_type_ids",
        }
    ]


def test_turn_expected_outcome_contract_preserves_workflow_concept_ids() -> None:
    contract = TurnExpectedOutcomeContract.from_mapping(
        {
            "expected_outcome_summary": "Represent the arXiv paper.",
            "required_tools": ["workflow_execute"],
            "target_workflow_id": "#V#arxiv_paper_representation_workflow",
            "workflow_concept_ids": [
                "#V#arxiv_paper_representation_workflow",
                "#V#scholarly_paper_representation_workflow",
            ],
        }
    )

    assert contract.workflow_concept_ids == (
        "#V#arxiv_paper_representation_workflow",
        "#V#scholarly_paper_representation_workflow",
    )

    payload = build_turn_expected_outcome_boundary_payload(contract)

    assert payload["turn_expected_workflow_concept_ids"] == [
        "#V#arxiv_paper_representation_workflow",
        "#V#scholarly_paper_representation_workflow",
    ]
    assert payload["turn_expected_outcome_profile"]["workflow_concept_ids"] == [
        "#V#arxiv_paper_representation_workflow",
        "#V#scholarly_paper_representation_workflow",
    ]
    assert payload["turn_expected_outcome_contract_state"]["workflow_concept_ids"] == [
        "#V#arxiv_paper_representation_workflow",
        "#V#scholarly_paper_representation_workflow",
    ]


def test_turn_expected_outcome_contract_ignores_execution_workflow_ids() -> None:
    contract = TurnExpectedOutcomeContract.from_mapping(
        {
            "summary": "Represent the paper.",
            "workflow_id": "#V#conversation_turn_execution_workflow",
            "selected_workflow_id": "#V#tool_calling_workflow",
        }
    )

    assert contract.workflow_concept_ids == ()


def test_turn_expected_outcome_contract_accepts_explicit_required_tools_context_key() -> (
    None
):
    contract = TurnExpectedOutcomeContract.from_mapping(
        {
            "summary": "Create represented labels and read them back.",
            "turn_expected_required_tools": [
                "create_concepts",
                "get_text_relations_summary",
                "create_concepts",
            ],
        }
    )

    assert contract.required_tools == (
        "create_concepts",
        "get_text_relations_summary",
    )

    payload = build_turn_expected_outcome_boundary_payload(contract)

    assert payload["turn_expected_required_tools"] == [
        "create_concepts",
        "get_text_relations_summary",
    ]
    assert payload["turn_expected_outcome_profile"]["required_tools"] == [
        "create_concepts",
        "get_text_relations_summary",
    ]
    assert payload["turn_expected_outcome_contract_state"]["required_tools"] == [
        "create_concepts",
        "get_text_relations_summary",
    ]


def test_turn_expected_outcome_contract_preserves_conditional_required_tools() -> None:
    contract = TurnExpectedOutcomeContract.from_mapping(
        {
            "summary": "Find a target, then mutate it if found.",
            "required_tools": ["gmail_list_messages"],
            "conditional_required_tools": ["workflow_execute"],
        }
    )

    assert contract.required_tools == ("gmail_list_messages",)
    assert contract.conditional_required_tools == ("workflow_execute",)

    payload = build_turn_expected_outcome_boundary_payload(contract)

    assert payload["turn_expected_required_tools"] == ["gmail_list_messages"]
    assert payload["turn_expected_conditional_required_tools"] == [
        "workflow_execute"
    ]
    assert payload["turn_expected_outcome_profile"]["conditional_required_tools"] == [
        "workflow_execute"
    ]
    assert payload["turn_expected_outcome_contract_state"][
        "conditional_required_tools"
    ] == ["workflow_execute"]


def test_turn_expected_outcome_contract_reads_nested_validated_json() -> None:
    contract = TurnExpectedOutcomeContract.from_mapping(
        {
            "outputs": {
                "validated_json": {
                    "summary": "Find a represented artefact target, then represent it.",
                    "required_tools": ["gmail_get_message"],
                    "conditional_required_tools": ["workflow_execute"],
                    "workflow_concept_ids": [
                        "#V#represented_artefact_creation_workflow"
                    ],
                    "target_contracts": [
                        {
                            "binding": "entity",
                            "target_description": (
                                "the represented artefact found in mail"
                            ),
                            "target_type_description": "represented artefact",
                        }
                    ],
                }
            }
        }
    )

    assert contract.required_tools == ("gmail_get_message",)
    assert contract.conditional_required_tools == ("workflow_execute",)
    assert contract.workflow_concept_ids == (
        "#V#represented_artefact_creation_workflow",
    )
    assert contract.target_contracts


def test_turn_expected_outcome_contract_preserves_hybrid_target_contract() -> None:
    contract = TurnExpectedOutcomeContract.from_mapping(
        {
            "summary": "Answer from a resolved target if available.",
            "target_contracts": [
                {
                    "kind": "hybrid",
                    "binding_kind": "entity",
                    "text": "the current project",
                    "candidate_concept_ids": ["#V#project_a", "#V#project_b"],
                    "resolution_status": "candidate_only",
                    "matching_policy": "exact",
                    "resolution_lineage": [{"source": "turn_context"}],
                }
            ],
        }
    )

    state = contract.to_state_payload()

    assert state["target_contracts"] == [
        {
            "schema_version": "turn_target_contract.v1",
            "kind": "hybrid",
            "binding_kind": "entity",
            "resolution_status": "candidate_only",
            "matching_policy": "exact",
            "text": "the current project",
            "candidate_concept_ids": ["#V#project_a", "#V#project_b"],
            "resolution_lineage": [{"source": "turn_context"}],
            "source": "target_contracts",
        }
    ]
