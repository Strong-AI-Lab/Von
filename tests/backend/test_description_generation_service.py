"""Tests for the description generation service."""

import pytest

from src.backend.services import description_generation_service as dgs


class TestIsPlaceholderDescription:
    """Test placeholder detection patterns."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            # ISO Timestamp-based placeholders
            ("2024-01-15T09:30:25", True),
            ("2024-01-15T09:30:25.123Z", True),
            # Short/minimal text (< 10 chars)
            ("abc", True),
            ("test", True),
            ("", True),
            (None, True),
            ("     ", True),
            ("short", True),
            # Snake_case derived from ID (entire string must be snake_case)
            ("my_concept_name", True),
            ("some_snake_case_text", True),
            # Concept ID prefix
            ("#V#some_concept", True),
            ("#V#workflow_stage", True),
            # Description fallback pattern
            ("Description fallback for concept", True),
            # Unnamed patterns
            ("Unnamed", True),
            ("Unnamed concept", True),
            ("Unnamed type", True),
            # No description patterns
            ("No description", True),
            ("No description available", True),
            # Valid descriptions (>= 10 chars, meaningful content)
            ("A well-written description of this concept.", False),
            ("This describes what the concept represents.", False),
            ("The purpose of this item is to track user preferences.", False),
            ("Short but meaningful.", False),
            ("This is fine.", False),  # Exactly 13 chars with punctuation
        ],
    )
    def test_is_placeholder_description(self, text, expected):
        """Test various placeholder patterns are detected correctly."""
        result = dgs.DescriptionGenerationService.is_placeholder_description(text)
        assert result == expected, f"Expected {expected} for text: {repr(text)}"

    def test_placeholder_with_whitespace_padding(self):
        """Test that padded short text is still detected."""
        result = dgs.DescriptionGenerationService.is_placeholder_description("   x   ")
        assert result is True

    def test_valid_description_with_underscore_word(self):
        """Underscores in context shouldn't trigger false positive."""
        # Single underscore word embedded in real text
        result = dgs.DescriptionGenerationService.is_placeholder_description(
            "This concept relates to user_id tracking in the system."
        )
        # Should be False because it's surrounded by real prose, not purely snake_case
        assert result is False


class TestPlaceholderPatterns:
    """Test the PLACEHOLDER_PATTERNS in the class."""

    def test_patterns_are_strings(self):
        """Verify patterns are regex strings (compiled at match time)."""
        for pattern in dgs.DescriptionGenerationService.PLACEHOLDER_PATTERNS:
            assert isinstance(pattern, str), f"Pattern not a string: {pattern}"

    def test_snake_case_pattern_specificity(self):
        """Snake case pattern should match entire snake_case strings."""
        # Single word too short
        assert (
            dgs.DescriptionGenerationService.is_placeholder_description("word") is True
        )
        # Multi-word sentence shouldn't match - mixed content
        assert (
            dgs.DescriptionGenerationService.is_placeholder_description(
                "This is a normal sentence with words."
            )
            is False
        )
