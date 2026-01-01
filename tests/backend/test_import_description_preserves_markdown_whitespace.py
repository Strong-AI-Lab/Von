import pytest

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from src.backend.security.access_control import bypass_access_control
from src.backend.services import text_value_service
from src.backend.vontology.utils_vontology import _ensure_import_description_relation


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


def test_import_description_relation_preserves_markdown_newlines_and_dedups():
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#import_desc_md_whitespace"
    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts.insert_one({"concept_id": concept_id, "relationships": {}})

    md = "## Heading\n\nLine 1\n\n- item 1\n- item 2\n"
    expected = "## Heading\n\nLine 1\n\n- item 1\n- item 2"

    with bypass_access_control():
        _ensure_import_description_relation(concept_id, md)

        texts = text_value_service.get_texts_for_concept(
            concept_id, predicate="hasDescription", limit=10
        )
        assert len(texts) == 1
        assert texts[0]["text"] == expected

        # Should not create a duplicate if only whitespace differs.
        md_variant = "  ## Heading\n\nLine 1\n\n- item 1\n- item 2\n\n"
        _ensure_import_description_relation(concept_id, md_variant)
        texts2 = text_value_service.get_texts_for_concept(
            concept_id, predicate="hasDescription", limit=10
        )
        assert len(texts2) == 1
        assert texts2[0]["text"] == expected
