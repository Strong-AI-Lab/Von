"""Tests for structural-field ↔ canonical-predicate consistency (JVNAUTOSCI-986).

These tests verify that:
1. Kind derivation is consistent (computed from structural relationship fields).
2. Structural fields (is_a_type_of, is_an_instance_of) match canonical predicate usage.
3. No drift occurs between structural and canonical predicate representations.
"""

from __future__ import annotations

import pytest

from src.backend.vontology.utils_vontology import (
    is_predicate,
    is_type,
    is_pure_instance,
)


class TestKindDerivationConsistency:
    """Test that kind is correctly derived from structural relationship fields."""

    def test_type_has_is_a_type_of(self):
        """A type concept must have is_a_type_of relationships."""
        node = {
            "concept_id": "#V#my_type",
            "relationships": {
                "is_a_type_of": ["#V#thing"],
                "is_an_instance_of": [],
            },
        }
        assert is_type(node) is True
        assert is_pure_instance(node) is False

    def test_individual_has_is_an_instance_of(self):
        """An individual concept has is_an_instance_of but no is_a_type_of."""
        node = {
            "concept_id": "#V#my_individual",
            "relationships": {
                "is_a_type_of": [],
                "is_an_instance_of": ["#V#person"],
            },
        }
        assert is_type(node) is False
        assert is_pure_instance(node) is True

    def test_predicate_is_instance_of_predicate_type(self):
        """A predicate concept is an instance of #V#predicate or similar."""
        node = {
            "concept_id": "#V#has_author",
            "relationships": {
                "is_a_type_of": [],
                "is_an_instance_of": ["#V#binary_predicate"],
            },
        }
        assert is_predicate(node) is True
        assert is_type(node) is False

    def test_higher_order_type_has_both_relationships(self):
        """A higher-order type may have both is_a_type_of and is_an_instance_of."""
        node = {
            "concept_id": "#V#academic_role",
            "relationships": {
                "is_a_type_of": ["#V#role"],
                "is_an_instance_of": ["#V#ontological_type"],
            },
        }
        # Has is_a_type_of, so is_type() returns True
        assert is_type(node) is True
        # Has is_an_instance_of but also is_a_type_of, so not a "pure" instance
        assert is_pure_instance(node) is False


class TestStructuralFieldNormalisation:
    """Test that structural relationship aliases are normalised correctly."""

    def test_v_prefixed_aliases_normalise_to_structural_keys(self):
        """#V#is_a_type_of should be treated as structural is_a_type_of."""
        from src.backend.services.relationship_write_service import (
            normalise_structural_predicate,
        )

        assert normalise_structural_predicate("#V#is_a_type_of") == "is_a_type_of"
        assert normalise_structural_predicate("#V#has_subtype") == "has_subtype"
        assert (
            normalise_structural_predicate("#V#is_an_instance_of")
            == "is_an_instance_of"
        )
        assert normalise_structural_predicate("#V#has_instance") == "has_instance"
        assert normalise_structural_predicate("#V#related_to") == "related_to"

    def test_non_structural_predicates_unchanged(self):
        """Non-structural predicates remain unchanged."""
        from src.backend.services.relationship_write_service import (
            normalise_structural_predicate,
        )

        assert normalise_structural_predicate("#V#has_author") == "#V#has_author"
        assert normalise_structural_predicate("custom_relation") == "custom_relation"


class TestDriftDetection:
    """Test that drift between structural and canonical representations is detected."""

    def test_no_drift_when_kinds_match(self):
        """No drift occurs when computed kind matches structural relationships."""
        from src.backend.services.relationship_write_service import (
            detect_kind_drift,
        )

        node = {
            "concept_id": "#V#test_type",
            "relationships": {
                "is_a_type_of": ["#V#thing"],
                "is_an_instance_of": [],
            },
        }
        # Expected kind is 'type', computed from is_a_type_of
        drift = detect_kind_drift(node, expected_kind="type")
        assert drift is None

    def test_drift_detected_when_kind_mismatch(self):
        """Drift is detected when expected kind doesn't match relationships."""
        from src.backend.services.relationship_write_service import (
            detect_kind_drift,
        )

        node = {
            "concept_id": "#V#test_type",
            "relationships": {
                "is_a_type_of": ["#V#thing"],
                "is_an_instance_of": [],
            },
        }
        # Expected kind is 'individual' but node is actually a type
        drift = detect_kind_drift(node, expected_kind="individual")
        assert drift is not None
        assert drift["expected"] == "individual"
        assert drift["computed"] == "type"

    def test_drift_detected_for_predicate_mismatch(self):
        """Drift is detected when predicate expected but not typed as such."""
        from src.backend.services.relationship_write_service import (
            detect_kind_drift,
        )

        node = {
            "concept_id": "#V#has_author",
            "relationships": {
                "is_a_type_of": [],
                "is_an_instance_of": ["#V#person"],  # Wrong! Should be #V#predicate
            },
        }
        drift = detect_kind_drift(node, expected_kind="predicate")
        assert drift is not None
        assert drift["expected"] == "predicate"
        assert drift["computed"] == "individual"
