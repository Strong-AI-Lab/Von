from src.backend.utils.concept_id_utils import (
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
