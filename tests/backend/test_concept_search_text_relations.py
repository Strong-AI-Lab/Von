import pytest

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from src.backend.security.access_control import bypass_access_control
from src.backend.services.concept_search_service import search_concepts
from src.backend.services.text_value_service import upsert_text_for_concept


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


def _insert_named_concept(
    concept_id: str,
    name: str,
    *,
    description: str | None = None,
) -> None:
    concepts = ConceptsRepository.collection()
    if concepts is None:
        return

    concepts.insert_one({"concept_id": concept_id, "relationships": {}})
    with bypass_access_control():
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasName",
            text=name,
            lang="en-NZ",
            context={"name_type": "NL"},
        )
        if isinstance(description, str) and description.strip():
            upsert_text_for_concept(
                subject_concept_id=concept_id,
                predicate="hasDescription",
                text=description,
                lang="en-NZ",
            )


def _has_concept(results, concept_id: str) -> bool:
    return any(r.get("concept_id") == concept_id for r in results or [])


def test_search_concepts_exact_match_is_case_insensitive_for_text_relations():
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#ai_researcher_example"
    _insert_named_concept(concept_id, "AI Researcher")

    with bypass_access_control():
        result = search_concepts(query="ai researcher", match_type="exact")

    assert _has_concept(result["results"], concept_id)


def test_search_concepts_exact_match_normalises_internal_whitespace():
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#ai_researcher_whitespace"
    _insert_named_concept(concept_id, "AI   Researcher")

    with bypass_access_control():
        result = search_concepts(query="AI Researcher", match_type="exact")

    assert _has_concept(result["results"], concept_id)


def test_search_concepts_substring_match_finds_has_description_relation():
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#description_search_relation_only"
    query = "authoritative relation search phrase"
    _insert_named_concept(
        concept_id,
        "Opaque Placeholder Name",
        description=f"This concept exists for {query}.",
    )

    with bypass_access_control():
        result = search_concepts(
            query=query,
            match_type="substring",
            include_description=True,
        )

    assert _has_concept(result["results"], concept_id)


def test_similarity_search_uses_canonical_description_text():
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#description_similarity_relation_only"
    query = "relation backed similarity phrase"
    _insert_named_concept(
        concept_id,
        "Unrelated Name",
        description=query,
    )

    with bypass_access_control():
        result = search_concepts(
            query=query,
            match_type="similarity",
            include_description=True,
            min_similarity=0.95,
        )

    assert _has_concept(result["results"], concept_id)
