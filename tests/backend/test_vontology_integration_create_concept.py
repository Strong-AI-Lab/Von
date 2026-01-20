import pytest

from bson import ObjectId
from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from src.backend.security.access_control import bypass_access_control
from src.backend.services.text_value_service import upsert_text_for_concept
from src.backend.vontology.utils_vontology import create_vontology_concept


@pytest.fixture(autouse=True)
def clean_collections():
    db = TextValuesRepository.db()
    if db is None:
        yield
        return

    concepts = ConceptsRepository.collection()
    relations = TextRelationsRepository.collection()
    text_values = TextValuesRepository.collection()

    if concepts is None or relations is None or text_values is None:
        yield
        return

    concepts.delete_many({})
    relations.delete_many({})
    text_values.delete_many({})
    yield
    concepts.delete_many({})
    relations.delete_many({})
    text_values.delete_many({})


def _seed_core_concepts(concepts):
    concepts.insert_one(
        {
            "concept_id": "#V#thing",
            "relationships": {
                "is_a_type_of": [],
                "is_an_instance_of": [],
            },
        }
    )
    concepts.insert_one(
        {
            "concept_id": "#V#person",
            "relationships": {
                "is_a_type_of": ["#V#thing"],
                "is_an_instance_of": [],
            },
        }
    )


def test_create_concept_and_text_relation_in_test_db():
    """Integration: create a concept and attach text relations in test DB."""
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    _seed_core_concepts(concepts)

    with bypass_access_control():
        result = create_vontology_concept(
            parent_id="#V#person",
            new_concept_name="Dr Hamid Abbasi",
            create_as_instance=True,
        )

        assert result.get("success") is True
        concept = result.get("concept") or {}
        concept_id = concept.get("concept_id")
        assert concept_id

        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text="Bioengineering researcher at the Auckland Bioengineering Institute.",
            lang="en-NZ",
        )

    relation = TextRelationsRepository.find_one(
        {"subject_concept_id": concept_id, "predicate": "hasDescription"}
    )
    assert relation is not None

    text_value_id = relation.get("object_text_id")
    assert isinstance(text_value_id, str)

    text_value = TextValuesRepository.find_one({"_id": ObjectId(text_value_id)})
    assert text_value is not None
    assert "Bioengineering researcher" in text_value.get("text", "")
