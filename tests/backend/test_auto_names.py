import logging
import unittest
from unittest.mock import MagicMock, patch

from src.backend.services.concept_service import create_concept
from src.backend.vontology.utils_vontology import import_ontology_nodes


class TestAutoNames(unittest.TestCase):
    @patch("src.backend.services.concept_service.ConceptsRepository")
    @patch("src.backend.services.text_value_service.upsert_text_for_concept")
    def test_create_concept_auto_names(self, mock_upsert, mock_repo):
        print("Testing create_concept_auto_names...")
        # Setup mock
        mock_collection = MagicMock()
        mock_repo.collection.return_value = mock_collection

        def side_effect_insert(doc):
            doc["_id"] = "mock_guid"
            res = MagicMock()
            res.inserted_id = "mock_guid"
            return res

        mock_collection.insert_one.side_effect = side_effect_insert

        # Mock find_one to return the created doc
        mock_doc = {
            "_id": "mock_guid",
            "concept_id": "#V#test_concept",
            "relationships": {},
        }
        mock_collection.find_one.return_value = mock_doc

        # Call create_concept
        create_concept(
            name="Test Concept", concept_id="#V#test_concept", create_as_instance=False
        )

        # Verify upsert calls
        calls = mock_upsert.call_args_list
        # print(f"Upsert calls: {len(calls)}")
        # for c in calls:
        #     print(f"  Call: {c.kwargs}")

        # Verify vonID call
        von_id_call = [
            c
            for c in calls
            if c.kwargs.get("text") == "#V#test_concept"
            and c.kwargs.get("context", {}).get("name_type") == "CODE"
        ]
        self.assertTrue(von_id_call, "vonID not registered as CODE name")

        # Verify GUID call
        guid_call = [
            c
            for c in calls
            if c.kwargs.get("text") == "mock_guid"
            and c.kwargs.get("context", {}).get("name_type") == "CODE"
        ]
        self.assertTrue(guid_call, "GUID not registered as CODE name")

    @patch(
        "src.backend.vontology.utils_vontology.ConceptsRepository"
    )  # Patch global import in utils_vontology
    @patch(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository"
    )  # Patch for local import
    @patch("src.backend.services.text_value_service.upsert_text_for_concept")
    def test_import_ontology_nodes_auto_names(
        self, mock_upsert, mock_repo_local, mock_repo_global
    ):
        print("\nTesting import_ontology_nodes_auto_names...")

        # Configure mocks
        # Both mocks should behave similarly for find/insert

        # Mock for local import (used in import_ontology_nodes main body)
        mock_repo_local.find_one.return_value = None  # No existing concept

        def side_effect_insert(doc):
            doc["_id"] = "mock_import_guid"
            return MagicMock()

        mock_repo_local.insert_one.side_effect = side_effect_insert

        # Mock for global import (used in detect_circular_references_in_import)
        # It calls find({}, projection)
        mock_repo_global.find.return_value = (
            []
        )  # No existing concepts for cycle detection

        nodes = [
            {
                "concept_id": "#V#imported_concept",
                "names": [
                    {"name": "Imported Concept", "language": "en-US", "type": "NL"}
                ],
            }
        ]

        # Call import
        import_ontology_nodes(nodes)

        # Verify upsert calls
        calls = mock_upsert.call_args_list
        # print(f"Upsert calls: {len(calls)}")
        # for c in calls:
        #     print(f"  Call: {c.kwargs}")

        # Verify vonID call
        von_id_call = [
            c
            for c in calls
            if c.kwargs.get("text") == "#V#imported_concept"
            and c.kwargs.get("context", {}).get("name_type") == "CODE"
        ]
        self.assertTrue(von_id_call, "vonID not registered as CODE name in import")

        # Verify GUID call
        guid_call = [
            c
            for c in calls
            if c.kwargs.get("text") == "mock_import_guid"
            and c.kwargs.get("context", {}).get("name_type") == "CODE"
        ]
        self.assertTrue(guid_call, "GUID not registered as CODE name in import")


@patch("src.backend.services.concept_service.ConceptsRepository")
@patch("src.backend.services.text_value_service.upsert_text_for_concept")
def test_create_concept_does_not_log_display_name(
    mock_upsert, mock_repo, caplog
):
    mock_collection = MagicMock()
    mock_repo.collection.return_value = mock_collection

    def side_effect_insert(doc):
        doc["_id"] = "mock_guid"
        result = MagicMock()
        result.inserted_id = "mock_guid"
        return result

    mock_collection.insert_one.side_effect = side_effect_insert
    mock_collection.find_one.return_value = {
        "_id": "mock_guid",
        "concept_id": "#V#private_person",
        "relationships": {},
    }

    private_display_name = "Private Person Display Name"
    with caplog.at_level(
        logging.INFO,
        logger="src.backend.services.concept_service",
    ):
        create_concept(
            name=private_display_name,
            concept_id="#V#private_person",
            create_as_instance=False,
        )

    assert private_display_name not in caplog.text


if __name__ == "__main__":
    unittest.main()
