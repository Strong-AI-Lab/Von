from src.backend.utils.concept_id_utils import (
    canonicalise_vontology_concept_id,
    ensure_v_concept_prefix,
    normalise_concept_id_for_compare,
    normalise_for_lookup,
    validate_concept_id_for_rename,
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


def test_canonicalise_vontology_concept_id_transliterates_accents() -> None:
    """JVNAUTOSCI-944: Accented characters should be transliterated to ASCII."""
    assert canonicalise_vontology_concept_id("#V#café") == "#V#cafe"
    assert canonicalise_vontology_concept_id("#V#naïve") == "#V#naive"
    assert (
        canonicalise_vontology_concept_id("#V#Sorbonne_Université")
        == "#V#sorbonne_universite"
    )
    assert canonicalise_vontology_concept_id("#V#señor") == "#V#senor"
    assert canonicalise_vontology_concept_id("#V#über") == "#V#uber"
    # Multi-character accents
    assert canonicalise_vontology_concept_id("#V#Ñoño") == "#V#nono"


def test_normalise_for_lookup_handles_unicode_normalisation() -> None:
    """JVNAUTOSCI-945: normalise_for_lookup uses NFKC normalisation."""
    # Composed vs decomposed forms should be equal
    assert normalise_for_lookup("café") == normalise_for_lookup("cafe\u0301")
    # Compatibility characters
    assert normalise_for_lookup("ﬁle") == normalise_for_lookup("file")
    # Case insensitive
    assert normalise_for_lookup("HELLO") == "hello"


def test_validate_concept_id_for_rename_valid_request() -> None:
    """JVNAUTOSCI-945: Valid rename request returns canonical new ID."""
    result, error = validate_concept_id_for_rename("#V#old_name", "#V#new_name")
    assert error is None
    assert result == "#V#new_name"


def test_validate_concept_id_for_rename_canonicalises() -> None:
    """JVNAUTOSCI-945: IDs are canonicalised before comparison."""
    result, error = validate_concept_id_for_rename("#V#Old-Name", "new name")
    assert error is None
    assert result == "#V#new_name"


def test_validate_concept_id_for_rename_rejects_same_id() -> None:
    """JVNAUTOSCI-945: Rejects rename when IDs are same after canonicalisation."""
    result, error = validate_concept_id_for_rename("#V#foo_bar", "Foo-Bar")
    assert result is None
    assert error is not None
    assert "same as current" in error.lower()


def test_validate_concept_id_for_rename_rejects_invalid_ids() -> None:
    """JVNAUTOSCI-945: Rejects invalid IDs."""
    # Invalid old ID
    result, error = validate_concept_id_for_rename("", "#V#new")
    assert result is None
    assert error is not None
    assert "invalid" in error.lower()

    # Invalid new ID
    result, error = validate_concept_id_for_rename("#V#old", "")
    assert result is None
    assert error is not None
    assert "invalid" in error.lower()


def test_validate_concept_id_for_rename_rejects_too_long() -> None:
    """JVNAUTOSCI-945: Rejects slugs exceeding max length."""
    long_slug = "a" * 250
    result, error = validate_concept_id_for_rename("#V#old", f"#V#{long_slug}")
    assert result is None
    assert error is not None
    assert "maximum length" in error.lower()
