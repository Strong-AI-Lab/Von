from src.backend.services.tool_target_contract_validation import (
    TARGET_CONTRACT_AMBIGUOUS_FOR_SYMBOLIC_TOOL,
    TARGET_CONTRACT_SYMBOLIC_MISMATCH,
    TARGET_CONTRACT_UNRESOLVED_FOR_SYMBOLIC_TOOL,
    successful_tool_result_concept_evidence,
    successful_tool_result_related_entity_evidence,
    target_contract_state_from_context,
    validate_tool_target_contract,
)
from src.backend.services.tool_metadata_service import (
    ToolRequiredObligationMetadata,
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


def test_natural_language_target_allows_bounded_symbolic_read_probe() -> None:
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

    assert result.ok is True
    assert result.diagnostics == ()
    assert result.resolution_evidence[0]["resolution_scope"] == (
        "bounded_unresolved_read_probe"
    )
    assert result.resolution_evidence[0]["preserves_unresolved_state"] is True
    assert result.resolution_evidence[0]["evidence"] == []


def test_resolved_focal_read_ignores_unresolved_secondary_type_contract() -> None:
    result = validate_tool_target_contract(
        tool_name="get_predicate_incidence",
        payload={"concept_id": "#V#michael_witbrock"},
        target_contract_state={
            "target_contracts": [
                {
                    "kind": "symbolic",
                    "binding_kind": "entity",
                    "concept_ids": ["#V#michael_witbrock"],
                    "resolution_status": "resolved",
                    "matching_policy": "exact",
                },
                {
                    "kind": "natural_language",
                    "binding_kind": "type",
                    "text": "PhD students supervised by the focal user",
                    "resolution_status": "unresolved",
                    "matching_policy": "exact",
                },
            ]
        },
    )

    assert result.ok is True
    assert result.diagnostics == ()
    assert len(result.resolution_evidence) == 1
    evidence = result.resolution_evidence[0]
    assert evidence["resolution_scope"] == (
        "resolved_read_target_with_unresolved_secondary_contracts"
    )
    assert evidence["planned_targets"] == [
        {"field": "concept_id", "value": "#V#michael_witbrock"}
    ]
    assert evidence["matched_resolved_target_contracts"][0]["concept_ids"] == [
        "#V#michael_witbrock"
    ]
    assert evidence["preserved_unresolved_target_contracts"][0]["text"] == (
        "PhD students supervised by the focal user"
    )
    assert evidence["preserves_unresolved_state"] is True
    assert evidence["evidence"] == []


def test_authenticated_actor_binding_allows_bounded_profile_lookup() -> None:
    target_state = target_contract_state_from_context(
        {
            "turn_expected_target_contracts": [
                {
                    "kind": "symbolic",
                    "binding_kind": "entity",
                    "text": "Authenticated user whose account must be resolved.",
                    "resolution_status": "unresolved",
                },
                {
                    "kind": "natural_language",
                    "binding_kind": "unknown",
                    "text": "the requested external account resource",
                    "resolution_status": "unresolved",
                },
            ],
            "target_concept_ids": ["#V#michael_witbrock"],
        }
    )

    result = validate_tool_target_contract(
        tool_name="find_relations_with_argument",
        payload={"concept_id": "#V#michael_witbrock"},
        target_contract_state=target_state,
    )

    assert result.ok is True
    assert result.diagnostics == ()
    assert result.resolution_evidence[0]["resolution_scope"] == (
        "resolved_read_target_with_unresolved_secondary_contracts"
    )
    assert result.resolution_evidence[0]["matched_resolved_target_contracts"][0][
        "concept_ids"
    ] == ["#V#michael_witbrock"]


def test_resolved_focal_mutation_still_blocks_unresolved_secondary_contract(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.tool_target_contract_validation."
        "get_tool_required_obligation_metadata",
        lambda _tool_name: ToolRequiredObligationMetadata(
            operation_class="mutation_write",
            target_argument_names=("concept_id",),
        ),
    )

    result = validate_tool_target_contract(
        tool_name="synthetic.update_target",
        payload={"concept_id": "#V#michael_witbrock"},
        target_contract_state={
            "target_contracts": [
                {
                    "kind": "symbolic",
                    "binding_kind": "entity",
                    "concept_ids": ["#V#michael_witbrock"],
                    "resolution_status": "resolved",
                    "matching_policy": "exact",
                },
                {
                    "kind": "natural_language",
                    "binding_kind": "type",
                    "text": "the unresolved result type",
                    "resolution_status": "unresolved",
                },
            ]
        },
    )

    assert result.ok is False
    assert result.first_error_code() == TARGET_CONTRACT_UNRESOLVED_FOR_SYMBOLIC_TOOL


def test_relation_grounded_related_entity_allows_verification_read() -> None:
    prior_invocations = [
        {
            "tool": "find_relations_with_argument",
            "status": "ok",
            "call_id": "call-relation-1",
            "effective_payload": {
                "success": True,
                "hits": [
                    {
                        "source_concept_id": "#V#michael_witbrock",
                        "predicate_concept_id": "#V#supervises_phd_student",
                        "target_concept_id": "#V#timothy_pistotti",
                        "target_concept_preview": {
                            "concept_id": "#V#timothy_pistotti",
                            "name": "Timothy Pistotti",
                        },
                    }
                ],
            },
        }
    ]

    result = validate_tool_target_contract(
        tool_name="fetch_concept",
        payload={"concept_id": "#V#timothy_pistotti"},
        target_contract_state={
            "target_contracts": [
                {
                    "kind": "symbolic",
                    "binding_kind": "entity",
                    "concept_ids": ["#V#michael_witbrock"],
                    "resolution_status": "resolved",
                    "matching_policy": "exact",
                }
            ]
        },
        prior_tool_invocations=prior_invocations,
    )

    assert result.ok is True
    assert result.diagnostics == ()
    assert result.resolution_evidence[0]["resolution_scope"] == (
        "grounded_related_entity_verification_read"
    )
    assert result.resolution_evidence[0]["preserves_target_agreement"] is True
    assert result.resolution_evidence[0]["evidence"] == [
        {
            "concept_id": "#V#timothy_pistotti",
            "tool": "find_relations_with_argument",
            "result_path": (
                "effective_payload.hits[0].target_concept_id"
            ),
            "call_id": "call-relation-1",
        },
        {
            "concept_id": "#V#timothy_pistotti",
            "tool": "find_relations_with_argument",
            "result_path": (
                "effective_payload.hits[0].target_concept_preview.concept_id"
            ),
            "call_id": "call-relation-1",
        },
    ]


def test_related_entity_evidence_excludes_predicates_metadata_and_prose() -> None:
    evidence = successful_tool_result_related_entity_evidence(
        [
            {
                "tool": "find_relations_with_argument",
                "status": "ok",
                "effective_payload": {
                    "summary": "Related to #V#prose_only",
                    "metadata": {"concept_id": "#V#metadata_only"},
                    "hits": [
                        {
                            "predicate_concept_id": "#V#predicate_only",
                            "target_concept_preview": {
                                "concept_id": "#V#grounded_related"
                            },
                        }
                    ],
                },
            }
        ]
    )

    assert evidence == (
        {
            "concept_id": "#V#grounded_related",
            "tool": "find_relations_with_argument",
            "result_path": (
                "effective_payload.hits[0].target_concept_preview.concept_id"
            ),
        },
    )


def test_relation_grounded_related_entity_does_not_authorise_mutation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.tool_target_contract_validation."
        "get_tool_required_obligation_metadata",
        lambda _tool_name: ToolRequiredObligationMetadata(
            operation_class="mutation_write",
            target_argument_names=("concept_id",),
        ),
    )

    result = validate_tool_target_contract(
        tool_name="synthetic.update_target",
        payload={"concept_id": "#V#timothy_pistotti"},
        target_contract_state={
            "target_contracts": [
                {
                    "kind": "symbolic",
                    "binding_kind": "entity",
                    "concept_ids": ["#V#michael_witbrock"],
                    "resolution_status": "resolved",
                    "matching_policy": "exact",
                }
            ]
        },
        prior_tool_invocations=[
            {
                "tool": "find_relations_with_argument",
                "status": "ok",
                "effective_payload": {
                    "hits": [
                        {"target_concept_id": "#V#timothy_pistotti"}
                    ]
                },
            }
        ],
    )

    assert result.ok is False
    assert result.first_error_code() == TARGET_CONTRACT_SYMBOLIC_MISMATCH


def test_natural_language_target_still_blocks_symbolic_mutation(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.backend.services.tool_target_contract_validation."
        "get_tool_required_obligation_metadata",
        lambda _tool_name: ToolRequiredObligationMetadata(
            operation_class="mutation_write",
            target_argument_names=("concept_id",),
        ),
    )

    result = validate_tool_target_contract(
        tool_name="synthetic.update_target",
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


def test_grounded_successful_tool_result_allows_provisional_verification_read() -> None:
    result = validate_tool_target_contract(
        tool_name="fetch_concept",
        payload={"concept_id": "#V#grounded_candidate"},
        target_contract_state={
            "target_contracts": [
                {
                    "kind": "natural_language",
                    "binding_kind": "entity",
                    "text": "the entity named by the user",
                    "resolution_status": "unresolved",
                }
            ]
        },
        prior_tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "call_id": "call-search-1",
                "effective_payload": {
                    "results": [{"concept_id": "#V#grounded_candidate"}]
                },
            }
        ],
    )

    assert result.ok is True
    assert result.diagnostics == ()
    assert result.resolution_evidence == (
        {
            "schema_version": "provisional_target_resolution.v1",
            "status": "valid",
            "tool": "fetch_concept",
            "resolution_scope": "verification_read_only",
            "planned_targets": [
                {"field": "concept_id", "value": "#V#grounded_candidate"}
            ],
            "evidence": [
                {
                    "concept_id": "#V#grounded_candidate",
                    "tool": "search_concepts",
                    "result_path": "effective_payload.results[0].concept_id",
                    "call_id": "call-search-1",
                }
            ],
        },
    )


def test_bounded_read_probe_does_not_claim_failed_or_prose_only_resolution() -> None:
    invocations = [
        {
            "tool": "search_concepts",
            "status": "error",
            "effective_payload": {"results": [{"concept_id": "#V#grounded_candidate"}]},
        },
        {
            "tool": "search_concepts",
            "status": "ok",
            "payload": {"concept_id": "#V#grounded_candidate"},
            "result_summary": "Found #V#grounded_candidate",
            "effective_payload": {
                "summary": "Found #V#grounded_candidate",
                "results": [{"concept_id": "#V#different_candidate"}],
            },
        },
    ]

    assert successful_tool_result_concept_evidence(invocations) == (
        {
            "concept_id": "#V#different_candidate",
            "tool": "search_concepts",
            "result_path": "effective_payload.results[0].concept_id",
        },
    )
    result = validate_tool_target_contract(
        tool_name="fetch_concept",
        payload={"concept_id": "#V#grounded_candidate"},
        target_contract_state={
            "target_contracts": [
                {
                    "kind": "natural_language",
                    "binding_kind": "entity",
                    "text": "the entity named by the user",
                    "resolution_status": "unresolved",
                }
            ]
        },
        prior_tool_invocations=invocations,
    )

    assert result.ok is True
    assert result.resolution_evidence[0]["resolution_scope"] == (
        "bounded_unresolved_read_probe"
    )
    assert result.resolution_evidence[0]["evidence"] == []


def test_success_status_does_not_override_failed_result_payload() -> None:
    evidence = successful_tool_result_concept_evidence(
        [
            {
                "tool": "search_concepts",
                "status": "ok",
                "effective_payload": {
                    "success": False,
                    "error_code": "search_failed",
                    "results": [{"concept_id": "#V#must_not_ground"}],
                },
            }
        ]
    )

    assert evidence == ()


def test_failed_nested_result_branch_does_not_ground_target() -> None:
    evidence = successful_tool_result_concept_evidence(
        [
            {
                "tool": "synthetic_batch_resolver",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "result": {
                        "success": False,
                        "error_code": "item_resolution_failed",
                        "concept_id": "#V#must_not_ground",
                    },
                    "resolved_concept_id": "#V#may_ground",
                },
            }
        ]
    )

    assert evidence == (
        {
            "concept_id": "#V#may_ground",
            "tool": "synthetic_batch_resolver",
            "result_path": "effective_payload.resolved_concept_id",
        },
    )


def test_structured_result_requires_exact_full_string_concept_id() -> None:
    evidence = successful_tool_result_concept_evidence(
        [
            {
                "tool": "search_concepts",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "id": "prefix #V#embedded_concept suffix",
                    "results": [{"concept_id": "#V#exact_concept"}],
                },
            }
        ]
    )

    assert evidence == (
        {
            "concept_id": "#V#exact_concept",
            "tool": "search_concepts",
            "result_path": "effective_payload.results[0].concept_id",
        },
    )


def test_successful_result_only_exposes_focal_concept_id_fields_as_evidence() -> (
    None
):
    evidence = successful_tool_result_concept_evidence(
        [
            {
                "tool": "synthetic_resolver",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "concept_id": "#V#focal_concept",
                    "focal_concept_id": "#V#explicit_focal_concept",
                    "resolved_concept_id": "#V#resolved_focal_concept",
                    "id": "#V#generic_record_id",
                    "instance_of": "#V#related_instance_type",
                    "parent_concept_id": "#V#related_parent",
                    "type_id": "#V#related_type",
                    "class_id": "#V#related_class",
                    "source_concept_id": "#V#related_source",
                    "target_concept_id": "#V#related_target",
                },
            }
        ]
    )

    assert evidence == (
        {
            "concept_id": "#V#focal_concept",
            "tool": "synthetic_resolver",
            "result_path": "effective_payload.concept_id",
        },
        {
            "concept_id": "#V#explicit_focal_concept",
            "tool": "synthetic_resolver",
            "result_path": "effective_payload.focal_concept_id",
        },
        {
            "concept_id": "#V#resolved_focal_concept",
            "tool": "synthetic_resolver",
            "result_path": "effective_payload.resolved_concept_id",
        },
    )


def test_nested_related_entities_do_not_become_focal_resolution_evidence() -> None:
    evidence = successful_tool_result_concept_evidence(
        [
            {
                "tool": "synthetic_resolver",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "concept_id": "#V#focal_concept",
                    "instance_of": {"concept_id": "#V#related_instance_type"},
                    "parent": {"concept_id": "#V#related_parent"},
                    "type": {"resolved_concept_id": "#V#related_type"},
                    "class": {"focal_concept_id": "#V#related_class"},
                    "source": {"concept_id": "#V#related_source"},
                    "target": {"concept_id": "#V#related_target"},
                },
            }
        ]
    )

    assert evidence == (
        {
            "concept_id": "#V#focal_concept",
            "tool": "synthetic_resolver",
            "result_path": "effective_payload.concept_id",
        },
    )


def test_nested_metadata_predicate_and_subject_ids_are_not_focal_evidence() -> None:
    evidence = successful_tool_result_concept_evidence(
        [
            {
                "tool": "synthetic_search",
                "status": "ok",
                "effective_payload": {
                    "success": True,
                    "metadata": {"concept_id": "#V#metadata_concept"},
                    "predicate": {
                        "resolved_concept_id": "#V#predicate_concept"
                    },
                    "subject": {"focal_concept_id": "#V#subject_concept"},
                    "results": [
                        {
                            "concept_id": "#V#legitimate_result",
                            "metadata": {
                                "concept_id": "#V#nested_metadata_concept"
                            },
                            "predicate": {
                                "concept_id": "#V#nested_predicate_concept"
                            },
                            "subject": {
                                "concept_id": "#V#nested_subject_concept"
                            },
                        }
                    ],
                },
            }
        ]
    )

    assert evidence == (
        {
            "concept_id": "#V#legitimate_result",
            "tool": "synthetic_search",
            "result_path": "effective_payload.results[0].concept_id",
        },
    )


def test_related_ids_do_not_become_resolution_evidence_for_bounded_read_probe() -> None:
    related_ids_by_field = {
        "id": "#V#generic_record_id",
        "instance_of": "#V#related_instance_type",
        "parent_concept_id": "#V#related_parent",
        "type_id": "#V#related_type",
        "class_id": "#V#related_class",
        "source_concept_id": "#V#related_source",
        "target_concept_id": "#V#related_target",
    }
    prior_tool_invocations = [
        {
            "tool": "synthetic_resolver",
            "status": "ok",
            "effective_payload": {
                "success": True,
                **related_ids_by_field,
            },
        }
    ]

    for related_id in related_ids_by_field.values():
        result = validate_tool_target_contract(
            tool_name="fetch_concept",
            payload={"concept_id": related_id},
            target_contract_state={
                "target_contracts": [
                    {
                        "kind": "natural_language",
                        "binding_kind": "entity",
                        "text": "the entity named by the user",
                        "resolution_status": "unresolved",
                    }
                ]
            },
            prior_tool_invocations=prior_tool_invocations,
        )

        assert result.ok is True, related_id
        assert result.resolution_evidence[0]["resolution_scope"] == (
            "bounded_unresolved_read_probe"
        )
        assert result.resolution_evidence[0]["evidence"] == []


def test_resolved_and_explicit_focal_ids_preserve_provisional_reads() -> None:
    for field_name in ("focal_concept_id", "resolved_concept_id"):
        result = validate_tool_target_contract(
            tool_name="fetch_concept",
            payload={"concept_id": "#V#grounded_candidate"},
            target_contract_state={
                "target_contracts": [
                    {
                        "kind": "natural_language",
                        "binding_kind": "entity",
                        "text": "the entity named by the user",
                        "resolution_status": "unresolved",
                    }
                ]
            },
            prior_tool_invocations=[
                {
                    "tool": "synthetic_resolver",
                    "status": "ok",
                    "effective_payload": {
                        "success": True,
                        field_name: "#V#grounded_candidate",
                    },
                }
            ],
        )

        assert result.ok is True, field_name
        assert result.resolution_evidence[0]["evidence"] == [
            {
                "concept_id": "#V#grounded_candidate",
                "tool": "synthetic_resolver",
                "result_path": f"effective_payload.{field_name}",
            }
        ]


def test_provisional_resolution_does_not_override_ambiguous_contract() -> None:
    result = validate_tool_target_contract(
        tool_name="fetch_concept",
        payload={"concept_id": "#V#candidate_a"},
        target_contract_state={
            "target_contracts": [
                {
                    "kind": "hybrid",
                    "binding_kind": "entity",
                    "candidate_concept_ids": ["#V#candidate_a", "#V#candidate_b"],
                    "resolution_status": "ambiguous",
                }
            ]
        },
        prior_tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "effective_payload": {
                    "results": [
                        {"concept_id": "#V#candidate_a"},
                        {"concept_id": "#V#candidate_b"},
                    ]
                },
            }
        ],
    )

    assert result.ok is False
    assert result.first_error_code() == TARGET_CONTRACT_AMBIGUOUS_FOR_SYMBOLIC_TOOL


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
