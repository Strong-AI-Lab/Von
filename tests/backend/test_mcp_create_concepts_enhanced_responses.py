"""
Tests for enhanced error responses in MCP create_concepts tool.

JVNAUTOSCI-1049: Validates that create_concepts returns detailed error information
including canonical IDs, input names, and actionable suggestions.
"""

import pytest
import uuid
from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.integrations.internal_mcp.catalogue import _create_concepts


@pytest.fixture(autouse=True)
def seed_core_concepts():
    """Seed #V#thing and #V#predicate for enhanced response tests."""
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
    # Cleanup test-created concepts
    concepts.delete_many(
        {"concept_id": {"$regex": "^#V#(enhanced_test_|already_exists_)"}}
    )


def test_create_concepts_already_exists_error_includes_enhanced_info():
    """
    Verify that "already exists" errors include canonical ID, input name, and suggestions.

    Context: JVNAUTOSCI-1049 - LLMs need explicit actionable feedback when a concept
    already exists, not just a message.
    """
    unique_name = f"enhanced_test_{uuid.uuid4().hex[:8]}"

    # First create a concept
    payload = {
        "parent_id": "#V#thing",
        "concepts": [{"name": unique_name, "kind": "type"}],
    }
    result1 = _create_concepts(**payload)
    assert result1.get("successful") == 1
    created_id = result1["results"][0].get("canonical_concept_id")
    assert created_id is not None

    # Try to create the same concept again
    result2 = _create_concepts(**payload)

    # Assert: should fail with enhanced error info
    assert result2.get("already_existed") == 1
    assert result2.get("successful") == 0

    error_result = result2["results"][0]
    assert error_result.get("success") is False
    assert error_result.get("error_code") == "already_exists"
    assert error_result.get("existing_concept_id") == created_id
    assert error_result.get("input_name") == unique_name
    assert "suggestion" in error_result
    assert "fetch_concept_content" in error_result["suggestion"]


def test_create_concepts_success_includes_canonical_id():
    """
    Verify that successful creation includes canonical_concept_id and input_name.

    This helps LLMs know the exact ID that was created, especially when the name
    differs from the canonical form.
    """
    # Use a name with spaces and mixed case that will be canonicalised
    original_name = "Enhanced Test Concept"
    unique_suffix = uuid.uuid4().hex[:8]
    full_name = f"{original_name} {unique_suffix}"

    payload = {
        "parent_id": "#V#thing",
        "concepts": [{"name": full_name, "kind": "type"}],
    }

    result = _create_concepts(**payload)

    # Assert: success with canonical info
    assert result.get("successful") == 1
    success_result = result["results"][0]
    assert success_result.get("success") is True
    assert "canonical_concept_id" in success_result
    assert success_result.get("input_name") == full_name
    # Canonical should be lowercase with underscores
    canonical = success_result["canonical_concept_id"]
    assert canonical.startswith("#V#")
    assert canonical.islower() or "_" in canonical  # Should be normalised


def test_create_concepts_result_includes_requested_name_and_kind():
    """
    Verify that each result includes the requested_name and requested_kind for traceability.
    """
    unique_name = f"enhanced_test_{uuid.uuid4().hex[:8]}"
    payload = {
        "parent_id": "#V#thing",
        "concepts": [
            {"name": unique_name, "kind": "type", "description": "A type"},
        ],
    }

    result = _create_concepts(**payload)

    # Assert: result includes requested fields
    item = result["results"][0]
    assert item.get("requested_name") == unique_name
    assert item.get("requested_kind") == "type"


def test_create_concepts_summary_includes_all_counts():
    """
    Verify that the summary includes total, successful, already_existed, and failed counts.
    """
    # Create a unique concept first
    existing_name = f"already_exists_{uuid.uuid4().hex[:8]}"
    _create_concepts(
        parent_id="#V#thing",
        concepts=[{"name": existing_name, "kind": "type"}],
    )

    # Now try to create multiple concepts including the existing one
    new_name = f"enhanced_test_{uuid.uuid4().hex[:8]}"
    payload = {
        "parent_id": "#V#thing",
        "concepts": [
            {"name": existing_name, "kind": "type"},  # Will fail - already exists
            {"name": new_name, "kind": "type"},  # Will succeed
        ],
    }

    result = _create_concepts(**payload)

    # Assert: summary includes all categories
    assert result.get("total") == 2
    assert result.get("successful") == 1
    assert result.get("already_existed") == 1
    assert result.get("failed") == 0
    assert "parent_id_used" in result


def test_create_concepts_includes_parent_id_used():
    """
    Verify that the response includes the canonicalised parent_id that was actually used.
    """
    unique_name = f"enhanced_test_{uuid.uuid4().hex[:8]}"
    payload = {
        "parent_id": "#V#Thing",  # Mixed case
        "concepts": [{"name": unique_name, "kind": "type"}],
    }

    result = _create_concepts(**payload)

    # Assert: parent_id_used is the canonicalised form
    assert result.get("parent_id_used") == "#V#thing"
