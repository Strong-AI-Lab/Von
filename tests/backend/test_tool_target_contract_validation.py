from src.backend.services.tool_target_contract_validation import (
    TARGET_CONTRACT_AMBIGUOUS_FOR_SYMBOLIC_TOOL,
    TARGET_CONTRACT_SYMBOLIC_MISMATCH,
    TARGET_CONTRACT_UNRESOLVED_FOR_SYMBOLIC_TOOL,
    target_contract_state_from_context,
    validate_tool_target_contract,
)


def test_symbolic_target_contract_accepts_exact_type_argument() -> None:
    result = validate_tool_target_contract(
        tool_name="get_predicate_incidence",
        payload={"instance_of": "#V#scientific_paper"},
        target_contract_state={
            "target_contracts": [
                {
                    "kind": "symbolic",
                    "binding_kind": "type",
                    "concept_ids": ["#V#scientific_paper"],
                    "resolution_status": "resolved",
                    "matching_policy": "exact",
                }
            ]
        },
    )

    assert result.ok is True
    assert result.diagnostics == ()


def test_symbolic_target_contract_rejects_wrong_entity_argument() -> None:
    result = validate_tool_target_contract(
        tool_name="get_predicate_incidence",
        payload={"concept_id": "#V#michael_witbrock"},
        target_contract_state={
            "target_contracts": [
                {
                    "kind": "symbolic",
                    "binding_kind": "type",
                    "concept_ids": ["#V#scientific_paper"],
                    "resolution_status": "resolved",
                    "matching_policy": "exact",
                }
            ]
        },
    )

    assert result.ok is False
    assert result.first_error_code() == TARGET_CONTRACT_SYMBOLIC_MISMATCH
    assert result.diagnostics[0]["planned_targets"] == [
        {"field": "concept_id", "value": "#V#michael_witbrock"}
    ]


def test_hierarchy_match_requires_explicit_contract_policy() -> None:
    exact_result = validate_tool_target_contract(
        tool_name="get_predicate_incidence",
        payload={"instance_of": "#V#scholarly_article"},
        target_contract_state={
            "target_contracts": [
                {
                    "kind": "symbolic",
                    "binding_kind": "type",
                    "concept_ids": ["#V#scholarly_work"],
                    "resolution_status": "resolved",
                    "matching_policy": "exact",
                }
            ]
        },
        hierarchy_match_resolver=lambda *_args: True,
    )
    subtype_result = validate_tool_target_contract(
        tool_name="get_predicate_incidence",
        payload={"instance_of": "#V#scholarly_article"},
        target_contract_state={
            "target_contracts": [
                {
                    "kind": "symbolic",
                    "binding_kind": "type",
                    "concept_ids": ["#V#scholarly_work"],
                    "resolution_status": "resolved",
                    "matching_policy": "exact_or_subtype",
                }
            ]
        },
        hierarchy_match_resolver=(
            lambda planned, expected, policy: (
                planned == "#V#scholarly_article"
                and expected == "#V#scholarly_work"
                and policy == "exact_or_subtype"
            )
        ),
    )

    assert exact_result.ok is False
    assert exact_result.first_error_code() == TARGET_CONTRACT_SYMBOLIC_MISMATCH
    assert subtype_result.ok is True


def test_natural_language_target_requires_resolution_before_symbolic_tool() -> None:
    result = validate_tool_target_contract(
        tool_name="find_relations_with_argument",
        payload={"concept_id": "#V#candidate_entity"},
        target_contract_state={
            "target_contracts": [
                {
                    "kind": "natural_language",
                    "binding_kind": "entity",
                    "text": "the candidate entity from the user's question",
                    "resolution_status": "unresolved",
                }
            ]
        },
    )

    assert result.ok is False
    assert result.first_error_code() == TARGET_CONTRACT_UNRESOLVED_FOR_SYMBOLIC_TOOL


def test_hybrid_candidate_target_preserves_uncertainty_and_blocks_symbolic_tool() -> (
    None
):
    result = validate_tool_target_contract(
        tool_name="find_relations_with_argument",
        payload={"concept_id": "#V#candidate_entity_a"},
        target_contract_state={
            "target_contracts": [
                {
                    "kind": "hybrid",
                    "binding_kind": "entity",
                    "text": "the project from context",
                    "candidate_concept_ids": [
                        "#V#candidate_entity_a",
                        "#V#candidate_entity_b",
                    ],
                    "resolution_status": "candidate_only",
                }
            ]
        },
    )

    assert result.ok is False
    assert result.first_error_code() == TARGET_CONTRACT_AMBIGUOUS_FOR_SYMBOLIC_TOOL
    target_contract = result.diagnostics[0]["target_contracts"][0]
    assert target_contract["candidate_concept_ids"] == [
        "#V#candidate_entity_a",
        "#V#candidate_entity_b",
    ]


def test_target_contract_state_from_context_merges_nested_and_top_level_targets() -> (
    None
):
    state = target_contract_state_from_context(
        {
            "turn_expected_outcome_contract_state": {
                "target_type_ids": ["#V#scholarly_article"]
            },
            "turn_expected_target_contracts": [
                {
                    "kind": "natural_language",
                    "binding_kind": "entity",
                    "text": "the unresolved focal entity",
                    "resolution_status": "unresolved",
                }
            ],
        }
    )

    assert state == {
        "schema_version": "turn_target_contract_context.v1",
        "target_contracts": [
            {
                "schema_version": "turn_target_contract.v1",
                "kind": "symbolic",
                "binding_kind": "type",
                "resolution_status": "resolved",
                "matching_policy": "exact",
                "concept_ids": ["#V#scholarly_article"],
                "source": "target_type_ids",
            },
            {
                "schema_version": "turn_target_contract.v1",
                "kind": "natural_language",
                "binding_kind": "entity",
                "resolution_status": "unresolved",
                "matching_policy": "exact",
                "text": "the unresolved focal entity",
                "source": "turn_expected_target_contracts",
            },
        ],
    }
