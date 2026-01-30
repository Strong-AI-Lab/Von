"""
Tests for parent_concept_ids canonicalisation in concept_service.create_concept.

JVNAUTOSCI-1051: Regression tests verifying that parent IDs are canonicalised
before being stored in is_a_type_of/is_an_instance_of relationships.
"""

import pytest
import uuid
from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.services.concept_service import create_concept


@pytest.fixture(autouse=True)
def seed_and_cleanup():
    """Seed core concepts and cleanup test concepts after each test."""
    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    # Seed #V#thing as root type
    concepts.delete_many({"concept_id": "#V#thing"})
    concepts.insert_one(
        {
            "concept_id": "#V#thing",
            "relationships": {"is_a_type_of": [], "is_an_instance_of": []},
        }
    )

    # Seed a parent type for testing: #V#business_travel
    concepts.delete_many({"concept_id": "#V#business_travel"})
    concepts.insert_one(
        {
            "concept_id": "#V#business_travel",
            "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
        }
    )

    yield

    # Cleanup test-created concepts
    concepts.delete_many(
        {"concept_id": {"$regex": "^#V#(parent_canon_test_|multi_parent_test_)"}}
    )


def test_create_concept_canonicalises_parent_ids_mixed_case():
    """
    Verify that create_concept canonicalises parent_concept_ids with mixed case.

    Context: JVNAUTOSCI-1047 - LLM uses "#V#Business_Travel" but should store "#V#business_travel".
    """
    unique_suffix = uuid.uuid4().hex[:8]
    concept_name = f"parent_canon_test_{unique_suffix}"

    # Act: create with mixed case parent ID
    result = create_concept(
        name=concept_name,
        concept_id=f"#V#{concept_name}",
        parent_concept_ids=["#V#Business_Travel"],  # Mixed case - should canonicalise
        create_as_instance=False,  # Type, so uses is_a_type_of
    )

    # Assert: stored relationship uses canonical form
    assert result is not None
    relationships = result.get("relationships", {})
    is_a_type_of = relationships.get("is_a_type_of", [])

    # Should contain canonical lowercase form
    assert "#V#business_travel" in is_a_type_of
    # Should NOT contain the original mixed-case form
    assert "#V#Business_Travel" not in is_a_type_of


def test_create_concept_canonicalises_parent_ids_with_spaces():
    """
    Verify that create_concept canonicalises parent_concept_ids with spaces.

    Input: "#V#Business Travel" → stored as "#V#business_travel"
    """
    unique_suffix = uuid.uuid4().hex[:8]
    concept_name = f"parent_canon_test_{unique_suffix}"

    result = create_concept(
        name=concept_name,
        concept_id=f"#V#{concept_name}",
        parent_concept_ids=["#V#Business Travel"],  # Space - should become underscore
        create_as_instance=False,
    )

    relationships = result.get("relationships", {})
    is_a_type_of = relationships.get("is_a_type_of", [])

    assert "#V#business_travel" in is_a_type_of


def test_create_concept_canonicalises_parent_ids_with_hyphens():
    """
    Verify that create_concept canonicalises parent_concept_ids with hyphens.

    Input: "#V#Business-Travel" → stored as "#V#business_travel"
    """
    unique_suffix = uuid.uuid4().hex[:8]
    concept_name = f"parent_canon_test_{unique_suffix}"

    result = create_concept(
        name=concept_name,
        concept_id=f"#V#{concept_name}",
        parent_concept_ids=["#V#Business-Travel"],  # Hyphen - should become underscore
        create_as_instance=False,
    )

    relationships = result.get("relationships", {})
    is_a_type_of = relationships.get("is_a_type_of", [])

    assert "#V#business_travel" in is_a_type_of


def test_create_concept_multiple_parent_variants_all_canonicalised():
    """
    Verify that all parent ID variants in a list get canonicalised consistently.

    Multiple different spellings of the same parent should all resolve to the same canonical form.
    """
    unique_suffix = uuid.uuid4().hex[:8]
    concept_name = f"multi_parent_test_{unique_suffix}"

    # Seed additional parent types for multi-parent test
    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")
    concepts.delete_many(
        {"concept_id": {"$in": ["#V#travel_expense", "#V#work_event"]}}
    )
    concepts.insert_one(
        {
            "concept_id": "#V#travel_expense",
            "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
        }
    )
    concepts.insert_one(
        {
            "concept_id": "#V#work_event",
            "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
        }
    )

    # Act: multiple parents with different variant spellings
    result = create_concept(
        name=concept_name,
        concept_id=f"#V#{concept_name}",
        parent_concept_ids=[
            "#V#Travel_Expense",  # Underscore + mixed case
            "#V#Work-Event",  # Hyphen + mixed case
            "#V#BUSINESS_TRAVEL",  # Uppercase
        ],
        create_as_instance=False,
    )

    relationships = result.get("relationships", {})
    is_a_type_of = relationships.get("is_a_type_of", [])

    # All should be canonical lowercase with underscores
    assert "#V#travel_expense" in is_a_type_of
    assert "#V#work_event" in is_a_type_of
    assert "#V#business_travel" in is_a_type_of

    # None should contain non-canonical forms (concept name part should be lowercase)
    for parent_id in is_a_type_of:
        # Check that name part after #V# is lowercase
        name_part = parent_id.replace("#V#", "")
        assert name_part == name_part.lower(), f"Non-canonical ID stored: {parent_id}"
        assert "-" not in parent_id, f"Hyphen in stored ID: {parent_id}"


def test_create_concept_as_instance_canonicalises_parent_ids():
    """
    Verify that parent canonicalisation works for instances (is_an_instance_of) too.

    When create_as_instance=True, parents go to is_an_instance_of, not is_a_type_of.
    """
    unique_suffix = uuid.uuid4().hex[:8]
    concept_name = f"parent_canon_test_{unique_suffix}"

    result = create_concept(
        name=concept_name,
        concept_id=f"#V#{concept_name}",
        parent_concept_ids=["#V#Business_Travel"],  # Mixed case
        create_as_instance=True,  # Instance, so uses is_an_instance_of
    )

    relationships = result.get("relationships", {})
    is_an_instance_of = relationships.get("is_an_instance_of", [])

    # Should contain canonical form
    assert "#V#business_travel" in is_an_instance_of
    # is_a_type_of should be empty for instances
    assert relationships.get("is_a_type_of", []) == []
