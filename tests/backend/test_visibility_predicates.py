from __future__ import annotations

from src.backend.security.visibility_predicates import (
    CANONICAL_SPECIFIC_TO_ORG_PREDICATE,
    CANONICAL_SPECIFIC_TO_USER_PREDICATE,
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


def test_set_visibility_values_writes_canonical_predicates_only() -> None:
    relationships = {
        "other_predicate": ["#V#value"],
        "specific_to_user": ["#V#legacy_user"],
        "specific_to_org": ["#V#legacy_org"],
    }

    updated = set_specific_to_org_values(relationships, ["#V#org_a"])
    updated = set_specific_to_user_values(updated, ["#V#user_a"])

    assert updated[CANONICAL_SPECIFIC_TO_ORG_PREDICATE] == ["#V#org_a"]
    assert updated[CANONICAL_SPECIFIC_TO_USER_PREDICATE] == ["#V#user_a"]
    assert "specific_to_org" not in updated
    assert "specific_to_user" not in updated
    assert updated["other_predicate"] == ["#V#value"]


def test_clearing_visibility_values_removes_canonical_and_legacy_family() -> None:
    relationships = {
        CANONICAL_SPECIFIC_TO_USER_PREDICATE: ["#V#user_a"],
        "specific_to_user": ["#V#legacy_user"],
    }

    updated = set_specific_to_user_values(relationships, [])

    assert CANONICAL_SPECIFIC_TO_USER_PREDICATE not in updated
    assert "specific_to_user" not in updated
