from __future__ import annotations

from src.backend.services.kr_relationship_readback_service import (
    verify_kr_relationship_readback,
    verify_relationship_effect_readback,
)


def _verify(**overrides):
    inputs = {
        "expected_source_id": "#V#trip",
        "expected_predicate_id": "#V#has_trip_component",
        "expected_target_id": "#V#flight_leg",
        "assertion_succeeded": True,
        "source_readback_id": "#V#trip",
        "source_readback_relationships": {"#V#has_trip_component": ["#V#flight_leg"]},
        "target_readback_id": "#V#flight_leg",
    }
    inputs.update(overrides)
    return verify_kr_relationship_readback(**inputs)


def test_verifies_the_exact_requested_relationship_tuple() -> None:
    result = _verify()

    assert result["success"] is True
    assert result["failure_code"] is None
    assert result["verified_relationship"] == {
        "source_id": "#V#trip",
        "predicate_id": "#V#has_trip_component",
        "target_id": "#V#flight_leg",
    }
    assert result["observed_readback"]["matched_predicate_keys"] == [
        "#V#has_trip_component"
    ]


def test_does_not_join_predicate_and_target_across_different_edges() -> None:
    result = _verify(
        source_readback_relationships={
            "#V#has_trip_component": ["#V#different_leg"],
            "#V#different_predicate": ["#V#flight_leg"],
        }
    )

    assert result["success"] is False
    assert result["failure_code"] == "kr_relationship_edge_readback_missing"
    assert "verified_relationship" not in result


def test_requires_both_endpoint_id_readbacks() -> None:
    source_mismatch = _verify(source_readback_id="#V#different_trip")
    target_mismatch = _verify(target_readback_id="#V#different_leg")

    assert source_mismatch["failure_code"] == (
        "kr_relationship_source_readback_mismatch"
    )
    assert target_mismatch["failure_code"] == (
        "kr_relationship_target_readback_mismatch"
    )


def test_requires_the_relationship_assertion_to_have_succeeded() -> None:
    result = _verify(assertion_succeeded=False)

    assert result["success"] is False
    assert result["failure_code"] == "kr_relationship_assertion_not_succeeded"


def test_accepts_the_canonical_storage_key_for_a_structural_predicate() -> None:
    result = _verify(
        expected_predicate_id="#V#is_a_type_of",
        source_readback_relationships={"is_a_type_of": ["#V#flight_leg"]},
    )

    assert result["success"] is True
    assert result["observed_readback"]["matched_predicate_keys"] == ["is_a_type_of"]


def test_rejects_an_invalid_expectation_without_claiming_verification() -> None:
    result = _verify(expected_predicate_id="")

    assert result["success"] is False
    assert result["failure_code"] == "kr_relationship_readback_expectation_invalid"
    assert "verified_relationship" not in result


def _effect_invocation(
    *,
    target_id: str = "#V#meeting",
    success: bool = True,
) -> dict[str, object]:
    return {
        "tool": "add_relationship",
        "status": "ok",
        "effective_arguments": {
            "source_id": "#V#file_copy",
            "predicate": "#V#documentary_evidence_for",
            "target": target_id,
        },
        "effective_payload": {
            "success": success,
            "effect_status": "succeeded" if success else "failed",
            "relationship_type": "concept_relation",
            "source_id": "#V#file_copy",
            "predicate": "#V#documentary_evidence_for",
            "predicate_input": "#V#documentary_evidence_for",
            "target": target_id,
            "added": success,
            "changed": success,
        },
    }


def _effect_hit(*, target_id: str = "#V#meeting") -> dict[str, object]:
    return {
        "access_granted": True,
        "canonical_publication": True,
        "is_asserted": True,
        "relation_state": "asserted",
        "source_concept_id": "#V#file_copy",
        "predicate_concept_id": "#V#documentary_evidence_for",
        "target_value": target_id,
        "relation_kind": "binary",
        "argument_indexes": [1],
        "relation_metadata": {
            "canonical_publication": True,
            "relation_id": "struct::documentary_evidence_for",
        },
    }


def _verify_effect(**overrides):
    inputs = {
        "mutation_tool_name": "add_relationship",
        "expected_source_id": "#V#file_copy",
        "expected_predicate_id": "#V#documentary_evidence_for",
        "expected_relation_kind": "binary",
        "tool_invocations": [_effect_invocation()],
        "readback_concept_id": "#V#file_copy",
        "readback_total_hits": 1,
        "readback_hits": [_effect_hit()],
        "readback_total_hits_is_lower_bound": False,
    }
    inputs.update(overrides)
    return verify_relationship_effect_readback(**inputs)


def test_relationship_effect_readback_correlates_result_confirmed_exact_edge() -> None:
    result = _verify_effect()

    assert result["relationship_effect_readback_verified"] is True
    assert result["relationship_effect_readback_failure_code"] is None
    assert result["relationship_effect_matching_mutation_count"] == 1
    assert result["represented_target_concept_id"] == "#V#meeting"
    assert result["verified_relationship"] == {
        "source_id": "#V#file_copy",
        "predicate_id": "#V#documentary_evidence_for",
        "target_id": "#V#meeting",
        "relation_kind": "binary",
        "relation_id": "struct::documentary_evidence_for",
    }
    assert result["verified_relationships"] == [result["verified_relationship"]]


def test_relationship_effect_readback_accepts_one_deterministic_action_receipt() -> (
    None
):
    result = _verify_effect(
        tool_invocations=None,
        relationship_effect_receipt=_effect_invocation(),
    )

    assert result["relationship_effect_readback_verified"] is True
    assert result["relationship_effect_matching_mutation_count"] == 1
    assert result["represented_target_concept_id"] == "#V#meeting"


def test_relationship_effect_readback_rejects_a_bare_deterministic_claim() -> None:
    result = _verify_effect(
        tool_invocations=None,
        relationship_effect_receipt={
            "tool": "add_relationship",
            "status": "ok",
            "effective_payload": {
                "success": True,
                "source_id": "#V#file_copy",
                "predicate": "#V#documentary_evidence_for",
                "target": "#V#meeting",
            },
        },
    )

    assert result["relationship_effect_readback_verified"] is False
    assert (
        result["relationship_effect_readback_failure_code"]
        == "relationship_effect_successful_mutation_missing"
    )


def test_relationship_effect_readback_requires_success_in_the_write_result() -> None:
    result = _verify_effect(tool_invocations=[_effect_invocation(success=False)])

    assert result["relationship_effect_readback_verified"] is False
    assert (
        result["relationship_effect_readback_failure_code"]
        == "relationship_effect_successful_mutation_missing"
    )
    assert result["represented_target_concept_id"] is None


def test_relationship_effect_readback_rejects_incoherent_write_containers() -> None:
    invocation = _effect_invocation()
    invocation["effective_arguments"] = {
        "source_id": "#V#file_copy",
        "predicate": "#V#documentary_evidence_for",
        "target": "#V#different_meeting",
    }

    result = _verify_effect(tool_invocations=[invocation])

    assert result["relationship_effect_readback_verified"] is False
    assert (
        result["relationship_effect_readback_failure_code"]
        == "relationship_effect_successful_mutation_missing"
    )


def test_relationship_effect_readback_rejects_explicitly_scoped_hit() -> None:
    scoped_hit = _effect_hit()
    scoped_hit["canonical_publication"] = False

    result = _verify_effect(readback_hits=[scoped_hit])

    assert result["relationship_effect_readback_verified"] is False
    assert (
        result["relationship_effect_readback_failure_code"]
        == "relationship_effect_exact_readback_missing"
    )


def test_relationship_effect_readback_requires_the_exact_readback_anchor() -> None:
    result = _verify_effect(readback_concept_id="#V#different_file_copy")

    assert result["relationship_effect_readback_verified"] is False
    assert (
        result["relationship_effect_readback_failure_code"]
        == "relationship_effect_readback_source_mismatch"
    )


def test_relationship_effect_readback_marks_a_partial_page_inconclusive() -> None:
    result = _verify_effect(
        readback_total_hits=2,
        readback_hits=[_effect_hit(target_id="#V#different_meeting")],
    )

    assert result["relationship_effect_readback_verified"] is False
    assert (
        result["relationship_effect_readback_failure_code"]
        == "relationship_effect_readback_incomplete"
    )


def test_relationship_effect_readback_verifies_every_unique_target_in_multi_mode() -> (
    None
):
    result = _verify_effect(
        allow_multiple_targets=True,
        minimum_unique_targets=2,
        tool_invocations=[
            _effect_invocation(target_id="#V#person_one"),
            _effect_invocation(target_id="#V#person_two"),
            _effect_invocation(target_id="#V#person_one"),
        ],
        readback_total_hits=2,
        readback_hits=[
            _effect_hit(target_id="#V#person_two"),
            _effect_hit(target_id="#V#person_one"),
        ],
    )

    assert result["relationship_effect_readback_verified"] is True
    assert result["relationship_effect_readback_failure_code"] is None
    assert result["relationship_effect_matching_mutation_count"] == 3
    assert result["relationship_effect_mutation_targets"] == [
        "#V#person_one",
        "#V#person_two",
    ]
    assert result["represented_target_concept_id"] is None
    assert result["verified_relationship"] is None
    assert [row["target_id"] for row in result["verified_relationships"]] == [
        "#V#person_one",
        "#V#person_two",
    ]


def test_relationship_effect_readback_multi_mode_requires_the_minimum_unique_targets() -> (
    None
):
    result = _verify_effect(
        allow_multiple_targets=True,
        minimum_unique_targets=2,
    )

    assert result["relationship_effect_readback_verified"] is False
    assert (
        result["relationship_effect_readback_failure_code"]
        == "relationship_effect_minimum_unique_targets_not_met"
    )
    assert result["represented_target_concept_id"] == "#V#meeting"
    assert result["verified_relationships"] == []


def test_relationship_effect_readback_multi_mode_rejects_one_missing_exact_hit() -> (
    None
):
    result = _verify_effect(
        allow_multiple_targets=True,
        minimum_unique_targets=2,
        tool_invocations=[
            _effect_invocation(target_id="#V#person_one"),
            _effect_invocation(target_id="#V#person_two"),
        ],
        readback_total_hits=1,
        readback_hits=[_effect_hit(target_id="#V#person_one")],
    )

    assert result["relationship_effect_readback_verified"] is False
    assert (
        result["relationship_effect_readback_failure_code"]
        == "relationship_effect_exact_readback_missing"
    )
    assert [row["target_id"] for row in result["verified_relationships"]] == [
        "#V#person_one"
    ]


def test_relationship_effect_readback_multi_mode_marks_missing_hit_on_partial_page_incomplete() -> (
    None
):
    result = _verify_effect(
        allow_multiple_targets=True,
        minimum_unique_targets=2,
        tool_invocations=[
            _effect_invocation(target_id="#V#person_one"),
            _effect_invocation(target_id="#V#person_two"),
        ],
        readback_total_hits=2,
        readback_total_hits_is_lower_bound=True,
        readback_hits=[_effect_hit(target_id="#V#person_one")],
    )

    assert result["relationship_effect_readback_verified"] is False
    assert (
        result["relationship_effect_readback_failure_code"]
        == "relationship_effect_readback_incomplete"
    )


def test_relationship_effect_readback_rejects_out_of_bounds_multi_target_minimum() -> (
    None
):
    result = _verify_effect(
        allow_multiple_targets=True,
        minimum_unique_targets=81,
    )

    assert result["relationship_effect_readback_verified"] is False
    assert (
        result["relationship_effect_readback_failure_code"]
        == "relationship_effect_readback_inputs_invalid"
    )
