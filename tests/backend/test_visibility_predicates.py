from __future__ import annotations

from src.backend.security.visibility_predicates import (
    get_specific_to_org_values,
    get_specific_to_user_values,
    set_specific_to_org_values,
    set_specific_to_user_values,
)


def test_collect_visibility_values_reads_legacy_and_canonical_variants() -> None:
    relationships = {
        "specific_to_org": ["#V#org_a"],
        "#V#specific_to_organisation": ["#V#org_b"],
        "specific_to_user": ["#V#user_a"],
        "#V#specific_to_user": ["#V#user_b"],
    }

    assert get_specific_to_org_values(relationships) == ["#V#org_a", "#V#org_b"]
    assert get_specific_to_user_values(relationships) == ["#V#user_a", "#V#user_b"]


def test_set_visibility_values_mirrors_across_supported_write_predicates() -> None:
    relationships = {"other_predicate": ["#V#value"]}

    updated = set_specific_to_org_values(relationships, ["#V#org_a"])
    updated = set_specific_to_user_values(updated, ["#V#user_a"])

    assert updated["specific_to_org"] == ["#V#org_a"]
    assert updated["specific_to_organisation"] == ["#V#org_a"]
    assert updated["#V#specific_to_organisation"] == ["#V#org_a"]
    assert updated["specific_to_user"] == ["#V#user_a"]
    assert updated["#V#specific_to_user"] == ["#V#user_a"]
    assert updated["other_predicate"] == ["#V#value"]

