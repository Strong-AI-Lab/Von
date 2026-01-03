from src.backend.utils.concept_id_utils import (
    canonicalise_vontology_concept_id,
    ensure_v_concept_prefix,
    normalise_concept_id_for_compare,
)


def test_normalise_concept_id_for_compare_handles_prefix_and_slug() -> None:
    assert normalise_concept_id_for_compare("#V#university") == "university"
    assert normalise_concept_id_for_compare("university") == "university"


def test_normalise_concept_id_for_compare_handles_blanks() -> None:
    assert normalise_concept_id_for_compare(None) is None
    assert normalise_concept_id_for_compare(123) is None
    assert normalise_concept_id_for_compare("") is None
    assert normalise_concept_id_for_compare("   ") is None


def test_ensure_v_concept_prefix_adds_prefix() -> None:
    assert ensure_v_concept_prefix("university") == "#V#university"
    assert ensure_v_concept_prefix("#university") == "#V#university"
    assert ensure_v_concept_prefix("#V#university") == "#V#university"


def test_ensure_v_concept_prefix_handles_blanks() -> None:
    assert ensure_v_concept_prefix(None) is None
    assert ensure_v_concept_prefix(123) is None
    assert ensure_v_concept_prefix("") is None
    assert ensure_v_concept_prefix("   ") is None


def test_canonicalise_vontology_concept_id_handles_hyphens_and_punctuation() -> None:
    assert canonicalise_vontology_concept_id("#V#foo-bar") == "#V#foo_bar"
    assert canonicalise_vontology_concept_id("#V#foo---bar") == "#V#foo_bar"
    assert canonicalise_vontology_concept_id("#V#foo bar") == "#V#foo_bar"
    assert canonicalise_vontology_concept_id("#V#foo...bar") == "#V#foo_bar"


def test_canonicalise_vontology_concept_id_coerces_prefix_and_case() -> None:
    assert canonicalise_vontology_concept_id("foo-bar") == "#V#foo_bar"
    assert canonicalise_vontology_concept_id("#v#Foo-Bar") == "#V#foo_bar"


def test_canonicalise_vontology_concept_id_handles_blanks() -> None:
    assert canonicalise_vontology_concept_id(None) is None
    assert canonicalise_vontology_concept_id(123) is None
    assert canonicalise_vontology_concept_id("") is None
    assert canonicalise_vontology_concept_id("   ") is None
