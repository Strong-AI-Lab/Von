"""
End-to-end tests for type hierarchy with variant ID spellings.

JVNAUTOSCI-1051: Verifies that creating parent types and subtypes with various
ID spelling variants results in correct type classification (not Individuals).
"""

import pytest
import uuid
from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.integrations.internal_mcp.catalogue import _create_concepts
from src.backend.services.concept_service import get_concept_by_concept_id


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

    yield

    # Cleanup test-created concepts
    concepts.delete_many(
        {"concept_id": {"$regex": "^#V#(hierarchy_test_|variant_subtype_)"}}
    )


def test_type_hierarchy_with_variant_ids_subtypes_are_types():
    """
    End-to-end test: Create parent type, then create subtypes with variant ID spellings.

    Verifies that:
    1. Parent type is created successfully
    2. Subtypes with various ID spellings (CamelCase, hyphens, spaces) are created
    3. All subtypes are classified as types (not individuals)
    4. All subtypes correctly reference the parent via canonical ID
    """
    unique_suffix = uuid.uuid4().hex[:8]
    parent_name = f"hierarchy_test_parent_{unique_suffix}"

    # Step 1: Create parent type
    parent_result = _create_concepts(
        parent_id="#V#thing",
        concepts=[{"name": parent_name, "kind": "type"}],
    )

    assert parent_result.get("successful") == 1
    first_result = parent_result["results"][0]
    assert isinstance(first_result, dict)
    parent_canonical_id = first_result.get("canonical_concept_id")
    assert parent_canonical_id is not None
    assert parent_canonical_id.startswith("#V#")

    # Step 2: Create subtypes with various ID spelling variants
    subtype_names = [
        f"variant_subtype_CamelCase_{unique_suffix}",
        f"variant_subtype_with_spaces_{unique_suffix}",
        f"variant-subtype-with-hyphens-{unique_suffix}",
    ]

    # Use variant spellings of parent ID that should canonicalise
    parent_variants = [
        parent_canonical_id.replace("#V#", "#V#").upper(),  # UPPERCASE
        parent_canonical_id.replace("_", "-"),  # hyphens
        parent_canonical_id.replace("_", " "),  # spaces
    ]

    for i, (subtype_name, parent_variant) in enumerate(
        zip(subtype_names, parent_variants)
    ):
        subtype_result = _create_concepts(
            parent_id=parent_variant,  # Variant spelling
            concepts=[{"name": subtype_name, "kind": "type"}],
        )

        # Should succeed via canonicalisation
        assert (
            subtype_result.get("successful") == 1
        ), f"Subtype {i} failed: {subtype_result}"
        assert subtype_result.get("error") is None

        # Verify the subtype was created with correct parent reference
        subtype_first = subtype_result["results"][0]
        assert isinstance(subtype_first, dict)
        subtype_canonical_id = subtype_first.get("canonical_concept_id")
        assert subtype_canonical_id is not None

        # Fetch the concept and verify it's a type (not individual)
        subtype_doc = get_concept_by_concept_id(subtype_canonical_id)
        assert subtype_doc is not None, f"Subtype {i} not found in DB"

        relationships = subtype_doc.get("relationships", {})

        # Should have is_a_type_of pointing to parent (canonical form)
        is_a_type_of = relationships.get("is_a_type_of", [])
        assert (
            parent_canonical_id in is_a_type_of
        ), f"Subtype {i} missing parent in is_a_type_of"

        # Should NOT be an instance (that would make it an individual)
        is_an_instance_of = relationships.get("is_an_instance_of", [])
        # Types can potentially have is_an_instance_of #V#type, but not the parent type
        if parent_canonical_id in is_an_instance_of:
            pytest.fail(
                f"Subtype {i} incorrectly has parent in is_an_instance_of (would be individual)"
            )


def test_type_hierarchy_variant_ids_parent_lookup_uses_canonical():
    """
    Verify that the parent_id_used in the response reflects canonicalisation.
    """
    unique_suffix = uuid.uuid4().hex[:8]
    parent_name = f"hierarchy_test_canonical_{unique_suffix}"

    # Create parent type
    parent_result = _create_concepts(
        parent_id="#V#thing",
        concepts=[{"name": parent_name, "kind": "type"}],
    )
    first_result = parent_result["results"][0]
    assert isinstance(first_result, dict)

    # Use a variant spelling of the parent
    variant_parent_id = f"#V#{parent_name}".replace("_", "-").upper()
    expected_canonical = f"#V#{parent_name}".lower().replace("-", "_")

    subtype_result = _create_concepts(
        parent_id=variant_parent_id,
        concepts=[{"name": f"subtype_{unique_suffix}", "kind": "type"}],
    )

    # The response should show the canonical parent ID that was actually used
    assert subtype_result.get("parent_id_used") == expected_canonical


def test_type_hierarchy_all_subtypes_have_consistent_parent_reference():
    """
    Create multiple subtypes in a batch using different parent ID variants.

    All should end up referencing the same canonical parent ID.
    """
    unique_suffix = uuid.uuid4().hex[:8]
    parent_name = f"hierarchy_test_batch_{unique_suffix}"

    # Create parent
    parent_result = _create_concepts(
        parent_id="#V#thing",
        concepts=[{"name": parent_name, "kind": "type"}],
    )
    first_parent = parent_result["results"][0]
    assert isinstance(first_parent, dict)
    parent_canonical_id = first_parent.get("canonical_concept_id")
    assert parent_canonical_id is not None

    # Create batch of subtypes using a variant parent ID
    variant_parent = parent_canonical_id.replace("_", " ")  # Use spaces
    subtypes = [
        {"name": f"batch_sub_a_{unique_suffix}", "kind": "type"},
        {"name": f"batch_sub_b_{unique_suffix}", "kind": "type"},
        {"name": f"batch_sub_c_{unique_suffix}", "kind": "type"},
    ]

    batch_result = _create_concepts(parent_id=variant_parent, concepts=subtypes)

    assert batch_result.get("successful") == 3

    # Verify all subtypes reference the canonical parent
    for result in batch_result["results"]:
        assert isinstance(result, dict)
        subtype_id = result.get("canonical_concept_id")
        assert subtype_id is not None
        subtype_doc = get_concept_by_concept_id(subtype_id)
        assert subtype_doc is not None

        is_a_type_of = subtype_doc.get("relationships", {}).get("is_a_type_of", [])
        assert (
            parent_canonical_id in is_a_type_of
        ), f"Subtype {subtype_id} has wrong parent reference"
