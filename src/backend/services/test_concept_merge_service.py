import unittest

from unittest.mock import patch
from src.backend.services.concept_merge_service import merge_concepts


class TestConceptMergeService(unittest.TestCase):
    @patch("src.backend.services.concept_merge_service.ConceptsRepository")
    @patch("src.backend.services.concept_merge_service.TextValuesRepository")
    @patch("src.backend.services.concept_merge_service.get_concept_by_id")
    def test_merge_concepts_simulate(
        self, mock_get_concept, mock_text_repo, mock_concepts_repo
    ):
        # Setup
        source_id = "#V#Source"
        target_id = "#V#Target"

        mock_get_concept.side_effect = lambda x: {
            "#V#Source": {
                "concept_id": "#V#Source",
                "name": "Source",
                "relationships": {"related_to": ["#V#Other"]},
                "names": [{"name": "Alias1"}],
            },
            "#V#Target": {
                "concept_id": "#V#Target",
                "name": "Target",
                "relationships": {},
                "names": [],
            },
        }.get(x)

        mock_concepts_repo.find.return_value = (
            []
        )  # No incoming relations for simplicity
        mock_text_repo.find.return_value = []

        # Execute
        report = merge_concepts(source_id, target_id, simulate=True)

        # Verify
        self.assertTrue(report["success"])
        self.assertTrue(report["simulate"])
        # Expecting: move_outgoing_relation (1), move_name (1 alias + 1 top name = 2), delete_source (1) = 4 operations
        # Wait, source name "Source" is different from target "Target", so it should be added as alias.
        # Alias1 is also added.
        # related_to -> #V#Other is added.
        # delete_source is added.
        # Total 4.

        ops = report["operations"]
        self.assertTrue(any(op["type"] == "move_outgoing_relation" for op in ops))
        self.assertTrue(
            any(op["type"] == "move_name" and op["name"] == "Alias1" for op in ops)
        )
        self.assertTrue(
            any(op["type"] == "move_name" and op["name"] == "Source" for op in ops)
        )
        self.assertTrue(any(op["type"] == "delete_source" for op in ops))


if __name__ == "__main__":
    unittest.main()
