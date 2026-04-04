"""
Tests for JVNAUTOSCI-689: instance_of_type parameter in create_concepts MCP tool.

This ensures that concepts can be created with BOTH:
- is_a_type_of relationship (hierarchy from parent_id)
- is_an_instance_of relationship (classification from instance_of_type)
"""

import pytest
from unittest.mock import patch, MagicMock

from src.backend.services.concept_service import create_concept


@pytest.fixture
def mock_db_collection():
    """Provide a mock MongoDB collection for testing."""
    mock_coll = MagicMock()
    mock_coll.insert_one.return_value = MagicMock(inserted_id="test_id")
    return mock_coll


class TestCreateConceptInstanceOfType:
    """Tests for instance_of_type parameter in create_concept."""

    @patch("src.backend.services.concept_service.ConceptsRepository")
    def test_instance_of_type_sets_both_relationships(
        self, mock_repo, mock_db_collection
    ):
        """When instance_of_type is provided, concept has both is_a_type_of and is_an_instance_of."""
        mock_repo.collection.return_value = mock_db_collection
        mock_repo.find_one.return_value = None  # No duplicate

        create_concept(
            name="published_in",
            concept_id="#V#published_in",
            parent_concept_ids=["#V#predicate"],
            create_as_instance=False,  # Would normally make it a subtype only
            instance_of_type="#V#nearly_functional_binary_predicate",
        )

        # Verify insert_one was called with the concept document
        assert mock_db_collection.insert_one.called
        inserted_doc = mock_db_collection.insert_one.call_args[0][0]

        # Check relationships - should have BOTH
        relationships = inserted_doc.get("relationships", {})
        assert relationships.get("is_a_type_of") == [
            "#V#predicate"
        ], "Should have is_a_type_of from parent_id"
        assert relationships.get("is_an_instance_of") == [
            "#V#nearly_functional_binary_predicate"
        ], "Should have is_an_instance_of from instance_of_type"

    @patch("src.backend.services.concept_service.ConceptsRepository")
    def test_instance_of_type_canonicalises_id(self, mock_repo, mock_db_collection):
        """instance_of_type value should be canonicalised."""
        mock_repo.collection.return_value = mock_db_collection
        mock_repo.find_one.return_value = None

        create_concept(
            name="test_predicate",
            concept_id="#V#test_predicate",
            parent_concept_ids=["#V#predicate"],
            create_as_instance=False,
            instance_of_type="#V#Nearly-Functional_binary_Predicate",  # Non-canonical form
        )

        inserted_doc = mock_db_collection.insert_one.call_args[0][0]
        relationships = inserted_doc.get("relationships", {})

        # Should be canonicalised to lowercase underscore form
        instance_of = relationships.get("is_an_instance_of", [])
        assert len(instance_of) == 1
        assert instance_of[0] == "#V#nearly_functional_binary_predicate"

    @patch("src.backend.services.concept_service.ConceptsRepository")
    def test_backward_compatible_without_instance_of_type(
        self, mock_repo, mock_db_collection
    ):
        """Without instance_of_type, existing behaviour is preserved."""
        mock_repo.collection.return_value = mock_db_collection
        mock_repo.find_one.return_value = None

        # Create as type (not instance) - should set is_a_type_of only
        create_concept(
            name="my_type",
            concept_id="#V#my_type",
            parent_concept_ids=["#V#thing"],
            create_as_instance=False,
            # No instance_of_type
        )

        inserted_doc = mock_db_collection.insert_one.call_args[0][0]
        relationships = inserted_doc.get("relationships", {})

        assert relationships.get("is_a_type_of") == ["#V#thing"]
        assert relationships.get("is_an_instance_of") == []

    @patch("src.backend.services.concept_service.ConceptsRepository")
    def test_backward_compatible_create_as_instance(
        self, mock_repo, mock_db_collection
    ):
        """create_as_instance=True without instance_of_type uses parent as instance type."""
        mock_repo.collection.return_value = mock_db_collection
        mock_repo.find_one.return_value = None

        # Create as instance - should set is_an_instance_of only
        create_concept(
            name="john_smith",
            concept_id="#V#john_smith",
            parent_concept_ids=["#V#person"],
            create_as_instance=True,
            # No instance_of_type
        )

        inserted_doc = mock_db_collection.insert_one.call_args[0][0]
        relationships = inserted_doc.get("relationships", {})

        assert relationships.get("is_a_type_of") == []
        assert relationships.get("is_an_instance_of") == ["#V#person"]

    @patch("src.backend.services.concept_service.ConceptsRepository")
    def test_instance_of_type_overrides_create_as_instance(
        self, mock_repo, mock_db_collection
    ):
        """When instance_of_type is provided, it overrides create_as_instance semantics."""
        mock_repo.collection.return_value = mock_db_collection
        mock_repo.find_one.return_value = None

        # Even with create_as_instance=True, instance_of_type takes precedence
        create_concept(
            name="special_predicate",
            concept_id="#V#special_predicate",
            parent_concept_ids=["#V#predicate"],
            create_as_instance=True,  # Would normally use parent as instance_of
            instance_of_type="#V#binary_predicate",  # But this takes precedence
        )

        inserted_doc = mock_db_collection.insert_one.call_args[0][0]
        relationships = inserted_doc.get("relationships", {})

        # instance_of_type should override: parent goes to is_a_type_of
        assert relationships.get("is_a_type_of") == ["#V#predicate"]
        assert relationships.get("is_an_instance_of") == ["#V#binary_predicate"]
