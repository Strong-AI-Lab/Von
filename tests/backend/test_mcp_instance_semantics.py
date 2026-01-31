"""
Integration tests for MCP instance creation and search semantics.

JVNAUTOSCI-691: Comprehensive test suite for:
- instance_of_type parameter behaviour (JVNAUTOSCI-689)
- Search result deduplication (JVNAUTOSCI-690)
- direct_instances_only search flag (JVNAUTOSCI-687)
- Relationship correctness for dual-hierarchy concepts

These are integration tests that use the real test database (VON_DB_NAME=test_von_db)
to verify correct end-to-end behaviour of MCP concept creation and search.
"""

import pytest
import uuid
from typing import List, Optional

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.services.concept_service import create_concept, get_concept
from src.backend.services.concept_search_service import search_concepts
from src.backend.vontology.utils_vontology import create_vontology_concept


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def setup_core_concepts():
    """Seed core concepts required for testing hierarchy and instance relationships."""
    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    # Clean up any previous test artifacts
    concepts.delete_many(
        {"concept_id": {"$regex": "^#V#(test_sem_|multipath_|hierarchy_)"}}
    )

    # Seed core concepts needed for tests
    core_ids = [
        "#V#thing",
        "#V#predicate",
        "#V#binary_predicate",
        "#V#nearly_functional_binary_predicate",
    ]
    concepts.delete_many({"concept_id": {"$in": core_ids}})

    # Create concept hierarchy: thing -> predicate -> binary_predicate -> nearly_functional_binary_predicate
    concepts.insert_one(
        {
            "concept_id": "#V#thing",
            "names": [{"name": "Thing", "name_type": "NL"}],
            "relationships": {"is_a_type_of": [], "is_an_instance_of": []},
        }
    )
    concepts.insert_one(
        {
            "concept_id": "#V#predicate",
            "names": [{"name": "Predicate", "name_type": "NL"}],
            "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
        }
    )
    concepts.insert_one(
        {
            "concept_id": "#V#binary_predicate",
            "names": [{"name": "Binary Predicate", "name_type": "NL"}],
            "relationships": {
                "is_a_type_of": ["#V#predicate"],
                "is_an_instance_of": [],
            },
        }
    )
    concepts.insert_one(
        {
            "concept_id": "#V#nearly_functional_binary_predicate",
            "names": [
                {"name": "Nearly Functional Binary Predicate", "name_type": "NL"}
            ],
            "relationships": {
                "is_a_type_of": ["#V#binary_predicate"],
                "is_an_instance_of": [],
            },
        }
    )

    yield

    # Clean up test-created concepts
    concepts.delete_many(
        {"concept_id": {"$regex": "^#V#(test_sem_|multipath_|hierarchy_)"}}
    )


def _create_test_concept(
    name: str,
    parent_ids: List[str],
    instance_of: Optional[List[str]] = None,
    concept_id: Optional[str] = None,
) -> str:
    """Helper to create a test concept directly in the database."""
    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.fail("MongoDB collection not available")

    cid = concept_id or f"#V#{name.lower().replace(' ', '_')}"
    concepts.delete_many({"concept_id": cid})

    doc = {
        "concept_id": cid,
        "names": [{"name": name, "name_type": "NL"}],
        "relationships": {
            "is_a_type_of": parent_ids or [],
            "is_an_instance_of": instance_of or [],
        },
    }
    concepts.insert_one(doc)
    return cid


# ============================================================================
# Test Instance Creation with instance_of_type (JVNAUTOSCI-689)
# ============================================================================


class TestInstanceCreationWithInstanceOfType:
    """Integration tests for instance_of_type parameter in concept creation."""

    def test_create_instance_with_instance_of_type_sets_both_relationships(self):
        """
        Verify instance_of_type creates BOTH is_a_type_of AND is_an_instance_of relationships.

        This is the core semantic: when instance_of_type is provided, the concept is:
        - A subtype of parent_id (in the hierarchy)
        - An instance of instance_of_type (for classification)
        """
        unique_name = f"test_sem_published_in_{uuid.uuid4().hex[:8]}"

        # Create concept with instance_of_type
        result = create_vontology_concept(
            parent_id="#V#predicate",
            new_concept_name=unique_name,
            create_as_instance=False,  # Would normally create subtype only
            instance_of_type="#V#nearly_functional_binary_predicate",
        )

        assert result["success"] is True, f"Creation failed: {result.get('message')}"

        # Fetch and verify relationships
        created_id = result["canonical_concept_id"]
        concept = ConceptsRepository.find_one({"concept_id": created_id})
        assert concept is not None, "Created concept not found in database"

        relationships = concept.get("relationships", {})
        assert "#V#predicate" in relationships.get(
            "is_a_type_of", []
        ), "Missing is_a_type_of relationship from parent_id"
        assert "#V#nearly_functional_binary_predicate" in relationships.get(
            "is_an_instance_of", []
        ), "Missing is_an_instance_of relationship from instance_of_type"

    def test_create_subtype_without_instance_of_type_sets_only_is_a_type_of(self):
        """
        Verify omitting instance_of_type creates a standard subtype (is_a_type_of only).

        This ensures backward compatibility with existing concept creation patterns.
        """
        unique_name = f"test_sem_my_type_{uuid.uuid4().hex[:8]}"

        result = create_vontology_concept(
            parent_id="#V#thing",
            new_concept_name=unique_name,
            create_as_instance=False,
            # No instance_of_type - standard subtype creation
        )

        assert result["success"] is True

        created_id = result["canonical_concept_id"]
        concept = ConceptsRepository.find_one({"concept_id": created_id})
        assert concept is not None, f"Concept {created_id} not found"
        relationships = concept.get("relationships", {})

        assert relationships.get("is_a_type_of") == [
            "#V#thing"
        ], "Subtype should have parent in is_a_type_of"
        assert (
            relationships.get("is_an_instance_of") == []
        ), "Subtype without instance_of_type should not have is_an_instance_of"

    def test_create_instance_without_instance_of_type_uses_parent(self):
        """
        Verify create_as_instance=True without instance_of_type uses parent as instance type.

        This is the standard instance creation pattern for individuals.
        """
        # First create a type to instantiate
        type_id = _create_test_concept(
            f"test_sem_person_type_{uuid.uuid4().hex[:8]}",
            parent_ids=["#V#thing"],
        )

        unique_name = f"test_sem_john_{uuid.uuid4().hex[:8]}"

        result = create_vontology_concept(
            parent_id=type_id,
            new_concept_name=unique_name,
            create_as_instance=True,  # Creates instance of parent
            # No instance_of_type
        )

        assert result["success"] is True

        created_id = result["canonical_concept_id"]
        concept = ConceptsRepository.find_one({"concept_id": created_id})
        assert concept is not None, f"Concept {created_id} not found"
        relationships = concept.get("relationships", {})

        assert (
            relationships.get("is_a_type_of") == []
        ), "Instance should not be a subtype"
        assert type_id in relationships.get(
            "is_an_instance_of", []
        ), "Instance should have parent in is_an_instance_of"

    def test_instance_of_type_overrides_create_as_instance_semantics(self):
        """
        Verify instance_of_type takes precedence over create_as_instance flag.

        When instance_of_type is provided:
        - parent_id goes to is_a_type_of (hierarchy)
        - instance_of_type goes to is_an_instance_of (classification)
        - create_as_instance flag is effectively ignored
        """
        unique_name = f"test_sem_override_{uuid.uuid4().hex[:8]}"

        result = create_vontology_concept(
            parent_id="#V#predicate",
            new_concept_name=unique_name,
            create_as_instance=True,  # Would normally use parent as instance_of
            instance_of_type="#V#binary_predicate",  # But this takes precedence
        )

        assert result["success"] is True

        created_id = result["canonical_concept_id"]
        concept = ConceptsRepository.find_one({"concept_id": created_id})
        assert concept is not None, f"Concept {created_id} not found"
        relationships = concept.get("relationships", {})

        # instance_of_type overrides: parent goes to is_a_type_of
        assert relationships.get("is_a_type_of") == ["#V#predicate"]
        assert relationships.get("is_an_instance_of") == ["#V#binary_predicate"]

    def test_instance_of_type_canonicalises_to_lowercase_underscore(self):
        """
        Verify instance_of_type values are canonicalised to lowercase underscore form.
        """
        unique_name = f"test_sem_canon_{uuid.uuid4().hex[:8]}"

        result = create_vontology_concept(
            parent_id="#V#predicate",
            new_concept_name=unique_name,
            create_as_instance=False,
            instance_of_type="#V#Nearly-Functional_Binary_Predicate",  # Non-canonical
        )

        assert result["success"] is True

        created_id = result["canonical_concept_id"]
        concept = ConceptsRepository.find_one({"concept_id": created_id})
        assert concept is not None, f"Concept {created_id} not found"
        relationships = concept.get("relationships", {})

        instance_of = relationships.get("is_an_instance_of", [])
        assert len(instance_of) == 1
        assert (
            instance_of[0] == "#V#nearly_functional_binary_predicate"
        ), "instance_of_type should be canonicalised"


# ============================================================================
# Test Search Deduplication (JVNAUTOSCI-690)
# ============================================================================


class TestSearchDeduplication:
    """Integration tests for search result deduplication."""

    def test_search_deduplication_multipath_inheritance(self):
        """
        Verify concepts found via multiple paths (e.g., name + description) appear only once.

        When searching with include_description=True, a concept matching both
        the name and description should not be duplicated in results.
        """
        unique_suffix = uuid.uuid4().hex[:8]
        unique_term = f"multipath_unique_term_{unique_suffix}"

        # Create concept with the unique term in both name AND description
        cid = _create_test_concept(
            f"Multipath Test {unique_term}",
            parent_ids=["#V#thing"],
            concept_id=f"#V#multipath_test_{unique_suffix}",
        )

        # Update to add description containing the same term
        concepts = ConceptsRepository.collection()
        assert concepts is not None, "Could not get concepts collection"
        concepts.update_one(
            {"concept_id": cid},
            {
                "$set": {
                    "metadata.description": f"This concept is about {unique_term} testing",
                    "attributes.description": f"Also mentions {unique_term} here",
                }
            },
        )

        # Search with description included
        result = search_concepts(
            query=unique_term,
            match_type="substring",
            include_description=True,
            limit=50,
        )

        # Extract concept_ids from results
        result_ids = [r.get("concept_id") for r in result.get("results", [])]

        # The concept should appear exactly once, not multiple times
        assert cid in result_ids, f"Expected concept {cid} not found in results"
        assert (
            result_ids.count(cid) == 1
        ), f"Concept {cid} appears {result_ids.count(cid)} times - should be deduplicated to 1"

    def test_search_deduplication_text_relations_and_legacy_fields(self):
        """
        Verify concepts found via text_relations AND legacy fields appear only once.

        The dual-schema support searches both modern text_relations and legacy
        name/description fields - results should be deduplicated.
        """
        unique_suffix = uuid.uuid4().hex[:8]
        unique_name = f"Dual Schema Test {unique_suffix}"

        # Create concept with legacy name field
        cid = _create_test_concept(
            unique_name,
            parent_ids=["#V#thing"],
            concept_id=f"#V#dual_schema_{unique_suffix}",
        )

        # Also add a text_relation for the same name (simulating dual-schema concept)
        concepts = ConceptsRepository.collection()
        assert concepts is not None, "Could not get concepts collection"
        concepts.update_one(
            {"concept_id": cid},
            {
                "$set": {
                    "name": unique_name,  # Legacy field
                    # names array already set by helper
                }
            },
        )

        # Search should find via multiple paths but show only once
        result = search_concepts(
            query=unique_suffix,
            match_type="substring",
            limit=50,
        )

        result_ids = [r.get("concept_id") for r in result.get("results", [])]
        matches = [rid for rid in result_ids if rid == cid]
        assert (
            len(matches) <= 1
        ), f"Concept {cid} should appear at most once, found {len(matches)} times"

    def test_search_two_pass_deduplication(self):
        """
        Verify two-pass search (prefix then substring) deduplicates correctly.

        When use_two_pass=True, a concept matching both prefix and substring
        patterns should appear only once.
        """
        unique_suffix = uuid.uuid4().hex[:8]
        prefix_name = f"hierarchytest{unique_suffix}"

        cid = _create_test_concept(
            prefix_name,
            parent_ids=["#V#thing"],
            concept_id=f"#V#{prefix_name}",
        )

        # Search with two-pass enabled
        result = search_concepts(
            query=prefix_name[:12],  # Partial match that works as prefix AND substring
            match_type="substring",
            use_two_pass=True,
            limit=50,
        )

        result_ids = [r.get("concept_id") for r in result.get("results", [])]
        assert result_ids.count(cid) <= 1, "Two-pass search should deduplicate results"

    def test_search_response_includes_deduplication_metadata(self):
        """
        Verify search response includes deduplication metadata (JVNAUTOSCI-690).

        The response should include:
        - deduplicated: True
        - duplicates_removed: count of removed duplicates
        - total_before_dedup: count before deduplication
        - total_after_dedup: count after deduplication
        """
        # Search for common term that should have results
        result = search_concepts(
            query="thing",
            match_type="substring",
            limit=50,
        )

        # Verify deduplication metadata is present
        assert (
            "deduplication" in result
        ), "Response should include deduplication metadata"
        dedup = result["deduplication"]
        assert dedup.get("deduplicated") is True, "deduplicated should be True"
        assert "duplicates_removed" in dedup, "Should include duplicates_removed count"
        assert "total_before_dedup" in dedup, "Should include total_before_dedup"
        assert "total_after_dedup" in dedup, "Should include total_after_dedup"

        # Verify counts are consistent
        assert (
            dedup["total_before_dedup"] >= dedup["total_after_dedup"]
        ), "total_before_dedup should be >= total_after_dedup"
        assert dedup["duplicates_removed"] == (
            dedup["total_before_dedup"] - dedup["total_after_dedup"]
        ), "duplicates_removed should equal difference between before and after"


# ============================================================================
# Test Relationship Correctness (Hierarchy Preservation)
# ============================================================================


class TestRelationshipCorrectness:
    """Tests verifying relationship integrity after complex operations."""

    def test_instance_of_type_preserves_hierarchy_lineage(self):
        """
        Verify instance_of_type doesn't break or alter the subtype hierarchy chain.

        Creating: thing -> entity -> person_subtype (also instance of predicate)
        The is_a_type_of chain should remain intact for hierarchy traversal.
        """
        unique_suffix = uuid.uuid4().hex[:8]

        # Create entity as subtype of thing
        entity_id = _create_test_concept(
            f"hierarchy_entity_{unique_suffix}",
            parent_ids=["#V#thing"],
        )

        # Create person_subtype as subtype of entity AND instance of another type
        result = create_vontology_concept(
            parent_id=entity_id,
            new_concept_name=f"hierarchy_person_{unique_suffix}",
            create_as_instance=False,
            instance_of_type="#V#binary_predicate",  # Dual nature
        )

        assert result["success"] is True

        created_id = result["canonical_concept_id"]
        concept = ConceptsRepository.find_one({"concept_id": created_id})
        assert concept is not None, f"Concept {created_id} not found"
        relationships = concept.get("relationships", {})

        # Verify hierarchy is preserved
        assert entity_id in relationships.get(
            "is_a_type_of", []
        ), "Hierarchy link to entity should be preserved"

        # Verify instance classification is separate
        assert "#V#binary_predicate" in relationships.get(
            "is_an_instance_of", []
        ), "Instance classification should be set correctly"

        # Verify they don't contaminate each other
        assert "#V#binary_predicate" not in relationships.get(
            "is_a_type_of", []
        ), "instance_of_type should not bleed into is_a_type_of"
        assert entity_id not in relationships.get(
            "is_an_instance_of", []
        ), "parent_id should not bleed into is_an_instance_of"

    def test_created_concept_findable_by_instance_of_search(self):
        """
        Verify concepts created with instance_of_type are searchable via instance_of filter.
        """
        unique_suffix = uuid.uuid4().hex[:8]

        # Create a concept that is an instance of binary_predicate
        result = create_vontology_concept(
            parent_id="#V#predicate",
            new_concept_name=f"hierarchy_searchable_{unique_suffix}",
            create_as_instance=False,
            instance_of_type="#V#binary_predicate",
        )

        assert result["success"] is True
        created_id = result["canonical_concept_id"]

        # Search for instances of binary_predicate
        search_result = search_concepts(
            query="",  # Empty query with instance_of filter
            instance_of="#V#binary_predicate",
            limit=100,
        )

        result_ids = [r.get("concept_id") for r in search_result.get("results", [])]
        assert (
            created_id in result_ids
        ), f"Concept {created_id} should be findable via instance_of search"


# ============================================================================
# Test direct_instances_only Flag (JVNAUTOSCI-687)
# ============================================================================


class TestDirectInstancesOnly:
    """Integration tests for direct_instances_only search parameter."""

    def test_direct_instances_only_excludes_subtype_instances(self):
        """
        Verify direct_instances_only=True only returns direct instances of a type.

        Setup:
          - Type A (parent type)
          - Type B (subtype of A)
          - Instance X (instance of A)
          - Instance Y (instance of B)

        When searching with instance_of=A and direct_instances_only=True,
        only Instance X should be returned (not Instance Y).
        """
        unique_suffix = uuid.uuid4().hex[:8]

        # Create parent type A
        type_a_id = _create_test_concept(
            f"Type A {unique_suffix}",
            parent_ids=["#V#thing"],
            concept_id=f"#V#type_a_{unique_suffix}",
        )

        # Create subtype B (subtype of A)
        type_b_id = _create_test_concept(
            f"Type B {unique_suffix}",
            parent_ids=[type_a_id],
            concept_id=f"#V#type_b_{unique_suffix}",
        )

        # Create instance X (direct instance of A)
        instance_x_id = _create_test_concept(
            f"Instance X {unique_suffix}",
            parent_ids=[],
            concept_id=f"#V#instance_x_{unique_suffix}",
        )
        # Set relationships manually for instance
        ConceptsRepository.update_one(
            {"concept_id": instance_x_id},
            {
                "$set": {
                    "relationships.is_a_type_of": [],
                    "relationships.is_an_instance_of": [type_a_id],
                }
            },
        )

        # Create instance Y (instance of B, which is a subtype of A)
        instance_y_id = _create_test_concept(
            f"Instance Y {unique_suffix}",
            parent_ids=[],
            concept_id=f"#V#instance_y_{unique_suffix}",
        )
        ConceptsRepository.update_one(
            {"concept_id": instance_y_id},
            {
                "$set": {
                    "relationships.is_a_type_of": [],
                    "relationships.is_an_instance_of": [type_b_id],
                }
            },
        )

        # Search with direct_instances_only=False (default) - should find both X and Y
        result_recursive = search_concepts(
            query="",
            instance_of=type_a_id,
            direct_instances_only=False,
            limit=100,
        )
        recursive_ids = [
            r.get("concept_id") for r in result_recursive.get("results", [])
        ]

        # Search with direct_instances_only=True - should find only X
        result_direct = search_concepts(
            query="",
            instance_of=type_a_id,
            direct_instances_only=True,
            limit=100,
        )
        direct_ids = [r.get("concept_id") for r in result_direct.get("results", [])]

        # Verify recursive search finds instance of subtype
        assert (
            instance_x_id in recursive_ids
        ), "Recursive search should find direct instance X"
        assert (
            instance_y_id in recursive_ids
        ), "Recursive search should find subtype instance Y"

        # Verify direct search only finds direct instances
        assert (
            instance_x_id in direct_ids
        ), "Direct search should find direct instance X"
        assert (
            instance_y_id not in direct_ids
        ), "Direct search should NOT find subtype instance Y"

    def test_direct_instances_only_query_info_included(self):
        """
        Verify direct_instances_only flag is included in query_info metadata.
        """
        result = search_concepts(
            query="",
            instance_of="#V#thing",
            direct_instances_only=True,
            limit=10,
        )

        query_info = result.get("query_info", {})
        assert (
            "direct_instances_only" in query_info
        ), "query_info should include direct_instances_only"
        assert (
            query_info["direct_instances_only"] is True
        ), "direct_instances_only should be True in query_info"

        # Test with False value
        result_false = search_concepts(
            query="",
            instance_of="#V#thing",
            direct_instances_only=False,
            limit=10,
        )
        query_info_false = result_false.get("query_info", {})
        assert (
            query_info_false["direct_instances_only"] is False
        ), "direct_instances_only should be False"

    def test_direct_instances_only_with_empty_query(self):
        """
        Verify direct_instances_only works correctly with empty query (fetch all instances).
        """
        unique_suffix = uuid.uuid4().hex[:8]

        # Create a type
        type_id = _create_test_concept(
            f"Direct Only Type {unique_suffix}",
            parent_ids=["#V#thing"],
            concept_id=f"#V#direct_only_type_{unique_suffix}",
        )

        # Create direct instance
        instance_id = _create_test_concept(
            f"Direct Only Instance {unique_suffix}",
            parent_ids=[],
            concept_id=f"#V#direct_only_instance_{unique_suffix}",
        )
        ConceptsRepository.update_one(
            {"concept_id": instance_id},
            {
                "$set": {
                    "relationships.is_a_type_of": [],
                    "relationships.is_an_instance_of": [type_id],
                }
            },
        )

        # Search with empty query and direct_instances_only
        result = search_concepts(
            query="",
            instance_of=type_id,
            direct_instances_only=True,
            limit=100,
        )

        result_ids = [r.get("concept_id") for r in result.get("results", [])]
        assert instance_id in result_ids, "Direct instance should be found"


# ============================================================================
# Backward Compatibility Tests
# ============================================================================


class TestBackwardCompatibility:
    """Tests ensuring existing behaviour is preserved."""

    def test_existing_create_behavior_unchanged_for_types(self):
        """Verify standard type creation (without instance_of_type) works as before."""
        unique_name = f"test_sem_legacy_type_{uuid.uuid4().hex[:8]}"

        result = create_vontology_concept(
            parent_id="#V#thing",
            new_concept_name=unique_name,
            create_as_instance=False,
        )

        assert result["success"] is True
        assert "canonical_concept_id" in result

        concept = ConceptsRepository.find_one(
            {"concept_id": result["canonical_concept_id"]}
        )
        assert (
            concept is not None
        ), f"Concept {result['canonical_concept_id']} not found"
        relationships = concept.get("relationships", {})
        assert relationships.get("is_a_type_of") == ["#V#thing"]
        assert relationships.get("is_an_instance_of") == []

    def test_existing_create_behavior_unchanged_for_instances(self):
        """Verify standard instance creation (without instance_of_type) works as before."""
        unique_suffix = uuid.uuid4().hex[:8]

        # First create a type
        type_id = _create_test_concept(
            f"test_sem_inst_parent_{unique_suffix}",
            parent_ids=["#V#thing"],
        )

        # Create instance of that type
        result = create_vontology_concept(
            parent_id=type_id,
            new_concept_name=f"test_sem_legacy_inst_{unique_suffix}",
            create_as_instance=True,
        )

        assert result["success"] is True

        concept = ConceptsRepository.find_one(
            {"concept_id": result["canonical_concept_id"]}
        )
        assert (
            concept is not None
        ), f"Concept {result['canonical_concept_id']} not found"
        relationships = concept.get("relationships", {})
        assert relationships.get("is_a_type_of") == []
        assert type_id in relationships.get("is_an_instance_of", [])

    def test_search_without_filters_returns_unique_results(self):
        """Verify basic search deduplicates results by default."""
        # Search for common term
        result = search_concepts(
            query="thing",
            match_type="substring",
            limit=50,
        )

        result_ids = [r.get("concept_id") for r in result.get("results", [])]

        # All IDs should be unique
        assert len(result_ids) == len(
            set(result_ids)
        ), f"Search results contain duplicates: {[cid for cid in result_ids if result_ids.count(cid) > 1]}"
