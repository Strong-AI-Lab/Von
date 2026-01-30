"""
Tests for parent existence validation in MCP create_concepts tool.

JVNAUTOSCI-1048: Validates that create_concepts rejects non-existent parent_id
and correctly canonicalises parent IDs.
"""

import pytest
import uuid
from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.integrations.internal_mcp.catalogue import _create_concepts


@pytest.fixture(autouse=True)
def seed_core_concepts():
    """Seed #V#thing and #V#predicate for parent validation tests."""
    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    # Seed core concepts needed for tests
    concepts.delete_many({"concept_id": {"$in": ["#V#thing", "#V#predicate"]}})
    concepts.insert_one(
        {
            "concept_id": "#V#thing",
            "relationships": {"is_a_type_of": [], "is_an_instance_of": []},
        }
    )
    concepts.insert_one(
        {
            "concept_id": "#V#predicate",
            "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
        }
    )
    yield
    # Cleanup test-created concepts (keep core for other tests)
    concepts.delete_many(
        {"concept_id": {"$regex": "^#V#(canon_parent_test_|orphan_concept_test)"}}
    )


def test_create_concepts_rejects_nonexistent_parent():
    """
    Verify that create_concepts returns a clear error when parent_id does not exist.

    Context: JVNAUTOSCI-1048 - Fail-fast validation prevents orphaned concepts with
    dangling parent references.
    """
    # Arrange: payload with a definitely non-existent parent
    nonexistent_parent = f"#V#nonexistent_parent_{uuid.uuid4().hex}"
    payload = {
        "parent_id": nonexistent_parent,
        "concepts": [
            {
                "name": "orphan_concept_test",
                "kind": "type",
                "description": "Should fail due to missing parent",
            }
        ],
    }

    # Act
    result = _create_concepts(**payload)

    # Assert: should return error with clear message
    assert "error" in result
    assert "not found" in result["error"].lower()
    assert result.get("error_code") == "parent_not_found"
    assert "canonical_parent_id" in result
    assert "original_parent_id" in result


def test_create_concepts_canonicalises_parent_id():
    """
    Verify that create_concepts canonicalises parent_id variants.

    Using #V#thing as a known-existing parent, test that various spellings
    (e.g., Thing, THING) are canonicalised to #V#thing and succeed.
    """
    unique_name = f"canon_parent_test_{uuid.uuid4().hex[:8]}"

    # Arrange: use variant spelling of a known parent
    payload = {
        "parent_id": "#V#Thing",  # Mixed case - should canonicalise to #V#thing
        "concepts": [
            {
                "name": unique_name,
                "kind": "type",
                "description": "Tests parent canonicalisation",
            }
        ],
    }

    # Act
    result = _create_concepts(**payload)

    # Assert: should succeed after canonicalisation
    assert result is not None
    assert "error" not in result
    assert result.get("total") == 1
    assert result.get("successful") == 1


def test_create_concepts_error_includes_both_parent_ids():
    """
    Verify that the error response includes both original and canonical parent IDs.

    This helps LLMs understand what transformation occurred and fix their input.
    """
    # Arrange: use a variant spelling that will canonicalise differently
    original_parent = "#V#NonExistent_Test_Parent"
    payload = {
        "parent_id": original_parent,
        "concepts": [{"name": "test", "kind": "type"}],
    }

    # Act
    result = _create_concepts(**payload)

    # Assert: error should include both IDs for debugging
    assert result.get("error_code") == "parent_not_found"
    assert result.get("original_parent_id") == original_parent
    # Canonical form should be lowercase with underscores
    assert result.get("canonical_parent_id") == "#V#nonexistent_test_parent"
