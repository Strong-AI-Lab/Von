"""
Test that create_concepts tolerates unknown top-level fields (e.g., namespace).

This validates the fix for the "Unexpected field 'namespace'" error that occurred
when the MCP orchestrator or other callers included extra context fields.
"""

import pytest
from src.backend.integrations.internal_mcp.catalogue import _create_concepts


def test_create_concepts_accepts_namespace_field():
    """
    Verify that create_concepts ignores unknown top-level fields like namespace.

    Context: JVNAUTOSCI-760 - MCP orchestrator was adding namespace to create_concepts
    calls, causing "Unexpected field 'namespace'" errors. The fix allows unknown fields
    to be ignored while still validating required fields.
    """
    import uuid

    # Arrange: payload with extra 'namespace' field and unique concept name
    unique_name = f"test_concept_ns_{uuid.uuid4().hex[:8]}"
    payload = {
        "parent_id": "#V#thing",
        "namespace": "#V#test_user",  # Extra field that should be ignored
        "concepts": [
            {
                "name": unique_name,
                "kind": "type",
                "description": "Test concept with namespace in payload",
            }
        ],
    }

    # Act: call create_concepts with extra field
    result = _create_concepts(**payload)

    # Assert: should succeed and create concept
    assert result is not None
    assert "results" in result
    assert "total" in result
    assert result["total"] == 1

    # Check that concept was created (will have success=True or concept_id)
    first_result = result["results"][0]
    assert isinstance(first_result, dict)
    # Either success=True or it has a concept_id (depends on backend implementation)
    assert first_result.get("success") is True or "concept_id" in first_result


def test_create_concepts_rejects_missing_required_fields():
    """
    Verify that create_concepts still validates required fields.

    Ensures that allowing unknown fields doesn't break validation of required fields.
    """
    # Arrange: payload missing required 'parent_id'
    payload = {
        "namespace": "#V#test_user",  # Extra field
        "concepts": [{"name": "test_concept", "kind": "type"}],
    }

    # Act & Assert: should fail due to missing required field
    result = _create_concepts(**payload)
    assert "error" in result
    assert "parent_id" in result["error"].lower()


def test_create_concepts_accepts_multiple_unknown_fields():
    """
    Verify that create_concepts tolerates multiple unknown fields.
    """
    # Arrange: payload with multiple extra fields
    payload = {
        "parent_id": "#V#thing",
        "namespace": "#V#test_user",
        "session_id": "abc123",
        "user_context": {"name": "Test User"},
        "concepts": [{"name": "multi_extra_test", "kind": "instance"}],
    }

    # Act
    result = _create_concepts(**payload)

    # Assert: should succeed
    assert result is not None
    assert result.get("total") == 1
    first_result = result["results"][0]
    assert isinstance(first_result, dict)
    assert first_result.get("success") is True or "concept_id" in first_result
