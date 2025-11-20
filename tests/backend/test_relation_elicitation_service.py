# src/backend/services/tests/test_relation_elicitation_service.py
import unittest
from unittest.mock import patch, MagicMock

from backend.services.relation_elicitation_service import RelationElicitationService

class TestRelationElicitationService(unittest.TestCase):

    @patch('backend.services.concept_service.get_concept_by_concept_id')
    @patch('backend.services.concept_service.get_concept_by_id')
    def test_get_elicitation_opportunities(self, mock_get_by_id, mock_get_by_concept_id):
        # Arrange
        instance_id = "instance_123"
        type_id = "#V#Person"

        mock_get_by_id.return_value = {
            "concept_id": instance_id,
            "name": "Test Person",
            "relationships": {
                "is_an_instance_of": [type_id],
                "existing_relation": ["some_value"]
            },
            "hypothesized_relations": {
                "hypothesized_relation": [{"value": "some_hypothesis"}]
            }
        }

        mock_get_by_concept_id.return_value = {
            "concept_id": type_id,
            "name": "Person",
            "relationships": {
                "suggested_relations_for_type": [
                    "existing_relation",
                    "hypothesized_relation",
                    "new_opportunity"
                ]
            }
        }

        service = RelationElicitationService()

        # Act
        opportunities = service.get_elicitation_opportunities(instance_id)

        # Assert
        self.assertEqual(opportunities, ["new_opportunity"])
        mock_get_by_id.assert_called_once_with(instance_id)
        mock_get_by_concept_id.assert_called_once_with(type_id)

if __name__ == '__main__':
    unittest.main()
