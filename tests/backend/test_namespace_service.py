"""
Unit tests for namespace_service.py

Tests namespace derivation, parsing, and validation for composite user@org namespaces.
"""

import pytest
import sys
from pathlib import Path
from typing import Any, cast

# Add parent directory to path to enable imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.backend.services.namespace_service import (
    coerce_namespace,
    derive_namespace,
    derive_namespace_for_actor,
    parse_namespace,
    is_org_scoped,
    get_user_id,
    get_org_id,
    normalize_namespace,
    resolve_canonical_namespace,
)


class TestDeriveNamespace:
    """Tests for derive_namespace function."""

    def test_derive_with_user_and_org(self):
        """Test deriving namespace with both user and org."""
        result = derive_namespace("michael_witbrock", "sail")
        assert result == "#V#michael_witbrock@sail"

    def test_derive_with_user_only(self):
        """Test deriving namespace with user only."""
        result = derive_namespace("michael_witbrock")
        assert result == "#V#michael_witbrock"

    def test_derive_with_none_org(self):
        """Test that None org is handled gracefully."""
        result = derive_namespace("michael_witbrock", None)
        assert result == "#V#michael_witbrock"

    def test_derive_with_role_parameter(self):
        """Test that role parameter is accepted (stored separately, not in namespace)."""
        result = derive_namespace("michael_witbrock", "sail", "admin")
        assert result == "#V#michael_witbrock@sail"

    def test_invalid_user_id_with_spaces(self):
        """Test that spaces in user_id raise ValueError."""
        with pytest.raises(ValueError, match="invalid characters"):
            derive_namespace("michael witbrock", "sail")

    def test_invalid_user_id_with_special_chars(self):
        """Test that special characters in user_id raise ValueError."""
        with pytest.raises(ValueError, match="invalid characters"):
            derive_namespace("michael-witbrock", "sail")

    def test_invalid_org_id_with_special_chars(self):
        """Test that special characters in org_id raise ValueError."""
        with pytest.raises(ValueError, match="invalid characters"):
            derive_namespace("michael_witbrock", "sail-org")

    def test_empty_user_id(self):
        """Test that empty user_id raises ValueError."""
        with pytest.raises(ValueError, match="user_id is required"):
            derive_namespace("")

    def test_none_user_id(self):
        """Test that None user_id raises ValueError."""
        with pytest.raises(ValueError, match="user_id is required"):
            derive_namespace(cast(Any, None))


class TestParseNamespace:
    """Tests for parse_namespace function."""

    def test_parse_org_scoped_namespace(self):
        """Test parsing organisation-scoped namespace."""
        result = parse_namespace("#V#michael_witbrock@sail")
        assert result == {"user_id": "michael_witbrock", "org_id": "sail"}

    def test_parse_user_only_namespace(self):
        """Test parsing user-only namespace."""
        result = parse_namespace("#V#michael_witbrock")
        assert result == {"user_id": "michael_witbrock", "org_id": None}

    def test_parse_invalid_prefix(self):
        """Test that missing #V# prefix raises ValueError."""
        with pytest.raises(ValueError, match="must start with #V#"):
            parse_namespace("michael_witbrock@sail")

    def test_parse_multiple_at_symbols(self):
        """Test that multiple @ symbols raise ValueError."""
        with pytest.raises(ValueError, match="multiple @ symbols"):
            parse_namespace("#V#michael@witbrock@sail")

    def test_parse_empty_user_id(self):
        """Test that namespace with empty user_id raises ValueError."""
        with pytest.raises(ValueError, match="Empty user_id or org_id"):
            parse_namespace("#V#@sail")

    def test_parse_empty_org_id(self):
        """Test that namespace with empty org_id raises ValueError."""
        with pytest.raises(ValueError, match="Empty user_id or org_id"):
            parse_namespace("#V#michael_witbrock@")

    def test_parse_none_namespace(self):
        """Test that None namespace raises ValueError."""
        with pytest.raises(ValueError, match="Invalid namespace"):
            parse_namespace(cast(Any, None))

    def test_parse_empty_string(self):
        """Test that empty string namespace raises ValueError."""
        with pytest.raises(ValueError, match="Invalid namespace"):
            parse_namespace("")


class TestIsOrgScoped:
    """Tests for is_org_scoped function."""

    def test_org_scoped_returns_true(self):
        """Test that org-scoped namespace returns True."""
        assert is_org_scoped("#V#michael_witbrock@sail") is True

    def test_user_only_returns_false(self):
        """Test that user-only namespace returns False."""
        assert is_org_scoped("#V#michael_witbrock") is False

    def test_invalid_namespace_returns_false(self):
        """Test that invalid namespace returns False without raising."""
        assert is_org_scoped("invalid") is False
        assert is_org_scoped("") is False


class TestGetUserId:
    """Tests for get_user_id function."""

    def test_extract_from_org_scoped(self):
        """Test extracting user_id from org-scoped namespace."""
        assert get_user_id("#V#michael_witbrock@sail") == "michael_witbrock"

    def test_extract_from_user_only(self):
        """Test extracting user_id from user-only namespace."""
        assert get_user_id("#V#michael_witbrock") == "michael_witbrock"

    def test_invalid_raises_error(self):
        """Test that invalid namespace raises ValueError."""
        with pytest.raises(ValueError):
            get_user_id("invalid")


class TestGetOrgId:
    """Tests for get_org_id function."""

    def test_extract_from_org_scoped(self):
        """Test extracting org_id from org-scoped namespace."""
        assert get_org_id("#V#michael_witbrock@sail") == "sail"

    def test_extract_from_user_only_returns_none(self):
        """Test that user-only namespace returns None."""
        assert get_org_id("#V#michael_witbrock") is None

    def test_invalid_raises_error(self):
        """Test that invalid namespace raises ValueError."""
        with pytest.raises(ValueError):
            get_org_id("invalid")


class TestNormalizeNamespace:
    """Tests for normalize_namespace function."""

    def test_normalize_org_scoped(self):
        """Test normalising org-scoped namespace."""
        result = normalize_namespace("#V#michael_witbrock@sail")
        assert result == "#V#michael_witbrock@sail"

    def test_normalize_user_only(self):
        """Test normalising user-only namespace."""
        result = normalize_namespace("#V#michael_witbrock")
        assert result == "#V#michael_witbrock"

    def test_normalize_invalid_raises_error(self):
        """Test that normalising invalid namespace raises ValueError."""
        with pytest.raises(ValueError):
            normalize_namespace("invalid")


class TestCompatibilityNormalisation:
    """Tests for compatibility helpers used at workflow write boundaries."""

    def test_coerce_legacy_slash_namespace(self):
        assert coerce_namespace("#V#user_alpha/#V#org_beta") == "#V#user_alpha@org_beta"

    def test_derive_namespace_for_actor_accepts_concept_ids(self):
        assert (
            derive_namespace_for_actor("#V#user_alpha", "#V#org_beta")
            == "#V#user_alpha@org_beta"
        )

    def test_resolve_canonical_namespace_prefers_explicit_canonicalised_value(self):
        assert (
            resolve_canonical_namespace(
                "#V#user_alpha/#V#org_beta",
                "#V#different_user",
                "#V#different_org",
            )
            == "#V#user_alpha@org_beta"
        )

    def test_resolve_canonical_namespace_derives_from_actor_when_missing(self):
        assert (
            resolve_canonical_namespace(None, "#V#user_alpha", "#V#org_beta")
            == "#V#user_alpha@org_beta"
        )


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_uppercase_characters_allowed(self):
        """Test that uppercase characters are allowed in namespace."""
        result = derive_namespace("Michael_Witbrock", "SAIL")
        assert result == "#V#Michael_Witbrock@SAIL"

    def test_numeric_characters_allowed(self):
        """Test that numeric characters are allowed in namespace."""
        result = derive_namespace("user123", "org456")
        assert result == "#V#user123@org456"

    def test_single_character_user_id(self):
        """Test that single character user_id is valid."""
        result = derive_namespace("a")
        assert result == "#V#a"

    def test_long_namespace(self):
        """Test that long namespace is handled correctly."""
        long_user = "a" * 100
        long_org = "b" * 100
        result = derive_namespace(long_user, long_org)
        assert result == f"#V#{long_user}@{long_org}"

    def test_roundtrip_org_scoped(self):
        """Test that namespace can be derived and parsed consistently."""
        original_user = "michael_witbrock"
        original_org = "sail"
        namespace = derive_namespace(original_user, original_org)
        parsed = parse_namespace(namespace)
        assert parsed["user_id"] == original_user
        assert parsed["org_id"] == original_org

    def test_roundtrip_user_only(self):
        """Test that user-only namespace roundtrips correctly."""
        original_user = "michael_witbrock"
        namespace = derive_namespace(original_user)
        parsed = parse_namespace(namespace)
        assert parsed["user_id"] == original_user
        assert parsed["org_id"] is None
