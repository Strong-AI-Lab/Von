from __future__ import annotations

from src.backend.services.kr_relationship_readback_service import (
    verify_kr_relationship_readback,
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
