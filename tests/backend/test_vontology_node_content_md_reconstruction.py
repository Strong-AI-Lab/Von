import pytest

from src.backend.vontology import utils_vontology


@pytest.fixture(autouse=True)
def clean_vontology_nodes_collection():
    db = utils_vontology.get_db()
    if db is not None:
        db["vontology_nodes"].delete_many({})
    yield
    if db is not None:
        db["vontology_nodes"].delete_many({})


def test_reconstructed_md_content_omits_boilerplate_metadata_fields():
    db = utils_vontology.get_db()
    if db is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#verified_entity_representation_agentic_workflow_design"

    db["vontology_nodes"].insert_one(
        {
            "concept_id": concept_id,
            "names": [
                {
                    "name": "Verified Entity Representation Agentic Workflow Design",
                    "type": "NL",
                    "language": "en-NZ",
                }
            ],
            "relationships": {
                "is_an_instance_of": ["#V#von_agentic_component_design_document"],
                "is_a_type_of": [],
            },
            # Intentionally no md_content.
            # Intentionally no source_concept.
            # Description/notes are stored in text relations in newer schema; here we just ensure fallback doesn't invent boilerplate.
        }
    )

    payload = utils_vontology.get_vontology_node_content(concept_id)
    assert "error" not in payload

    md = payload.get("md_content")
    assert isinstance(md, str)

    assert md.startswith("# ")
    assert "Source Concept" not in md
    assert "SubConcept Of" not in md
    assert "Instance Of" not in md
