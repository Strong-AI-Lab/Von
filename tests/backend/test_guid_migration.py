import unittest
import uuid
from unittest.mock import MagicMock, patch

from src.backend.services.concept_service import (
    create_concept,
    update_concept,
    InvalidConceptDataError,
)
from src.utilities.migrate_guids import migrate_guids


class TestGuidMigration(unittest.TestCase):
    @patch("src.backend.services.concept_service.ConceptsRepository")
    @patch("src.backend.services.text_value_service.upsert_text_for_concept")
    def test_create_concept_generates_guid(self, mock_upsert, mock_repo):
        print("Testing create_concept_generates_guid...")
        mock_collection = MagicMock()
        mock_repo.collection.return_value = mock_collection

        # Capture the doc passed to insert_one
        captured_doc = {}

        def side_effect_insert(doc):
            captured_doc.update(doc)
            doc["_id"] = "mock_oid"
            res = MagicMock()
            res.inserted_id = "mock_oid"
            return res

        mock_collection.insert_one.side_effect = side_effect_insert

        # Mock find_one to return the captured doc
        def side_effect_find_one(query):
            if query.get("_id") == "mock_oid":
                return captured_doc
            return None

        mock_collection.find_one.side_effect = side_effect_find_one

        # Call create_concept
        result = create_concept(
            name="New Concept", concept_id="#V#new_concept", create_as_instance=False
        )

        # Verify result has guid
        self.assertIn("guid", result)
        # Verify it looks like a UUID
        try:
            uuid.UUID(result["guid"])
        except ValueError:
            self.fail("Generated guid is not a valid UUID")

        # Verify upsert called for guid
        calls = mock_upsert.call_args_list
        guid_call = [
            c
            for c in calls
            if c.kwargs.get("text") == result["guid"]
            and c.kwargs.get("context", {}).get("name_type") == "CODE"
        ]
        self.assertTrue(guid_call, "GUID not registered as CODE name")

    @patch("src.backend.services.concept_service.ConceptsRepository")
    def test_update_concept_immutability(self, mock_repo):
        print("Testing update_concept_immutability...")
        mock_collection = MagicMock()
        mock_repo.collection.return_value = mock_collection

        # Try to update guid
        with self.assertRaises(InvalidConceptDataError):
            update_concept("some_id", {"guid": "new_guid"})

    @patch("src.utilities.migrate_guids.get_concepts_collection")
    @patch("src.utilities.migrate_guids.upsert_text_for_concept")
    def test_migration_script(self, mock_upsert, mock_get_coll):
        print("Testing migration_script...")
        mock_collection = MagicMock()
        mock_get_coll.return_value = mock_collection

        # Mock concepts without GUID
        mock_concepts = [
            {"_id": "doc1", "concept_id": "#V#c1"},
            {"_id": "doc2", "concept_id": "#V#c2"},
        ]
        mock_collection.find.return_value = mock_concepts
        mock_collection.count_documents.return_value = 2

        # Run migration
        migrate_guids()

        # Verify update_one called twice with a guid
        self.assertEqual(mock_collection.update_one.call_count, 2)

        # Check args of first update
        args, _ = mock_collection.update_one.call_args_list[0]
        filter_arg, update_arg = args
        self.assertEqual(filter_arg["_id"], "doc1")
        self.assertIn("guid", update_arg["$set"])

        # Verify upsert called twice
        self.assertEqual(mock_upsert.call_count, 2)


if __name__ == "__main__":
    unittest.main()
