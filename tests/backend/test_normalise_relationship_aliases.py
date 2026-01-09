from __future__ import annotations

from src.utilities.normalise_relationship_aliases import normalise_relationship_aliases


def test_normalise_relationship_aliases_merges_plural_keys():
    rel = {
        "has_subtypes": ["#V#child_a"],
        "has_subtype": ["#V#child_b"],
        "is_a_type_ofs": "#V#parent",
        "related_tos": ["#V#peer"],
    }

    normalised, applied = normalise_relationship_aliases(rel)

    assert applied == {
        "has_subtypes": "has_subtype",
        "is_a_type_ofs": "is_a_type_of",
        "related_tos": "related_to",
    }
    assert "has_subtypes" not in normalised
    assert "is_a_type_ofs" not in normalised
    assert "related_tos" not in normalised
    assert normalised["has_subtype"] == ["#V#child_b", "#V#child_a"]
    assert normalised["is_a_type_of"] == ["#V#parent"]
    assert normalised["related_to"] == ["#V#peer"]


def test_normalise_relationship_aliases_drops_empty_alias():
    rel = {"has_instances": []}

    normalised, applied = normalise_relationship_aliases(rel)

    assert applied == {"has_instances": "has_instance"}
    assert "has_instances" not in normalised
    assert "has_instance" not in normalised
