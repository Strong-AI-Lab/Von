import pytest

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from src.backend.security.access_control import bypass_access_control
from src.backend.services.text_value_service import (
    get_text_relations_summary,
    upsert_singleton_text_relation,
    upsert_text_for_concept,
)


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


def test_get_text_relations_summary_groups_and_caps_relation_ids():
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#text_summary_test_concept"
    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts.insert_one({"concept_id": concept_id, "relationships": {}})

    with bypass_access_control():
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text="first",
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text="second",
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text="troisième",
            lang="fr",
        )

        summary = get_text_relations_summary(
            concept_id,
            max_relation_ids_per_group=1,
        )

    assert summary["success"] is True
    assert summary["concept_id"] == concept_id
    assert summary["groups_found"] == 2
    assert summary["total_relations_scanned"] == 3

    groups = {(g["predicate"], g["language"]): g for g in summary["groups"]}
    en_group = groups[("hasDescription", "en-NZ")]
    fr_group = groups[("hasDescription", "fr")]

    assert en_group["count"] == 2
    assert fr_group["count"] == 1

    assert len(en_group["relation_ids"]) <= 1
    assert len(fr_group["relation_ids"]) <= 1


def test_upsert_singleton_text_relation_replaces_others_for_language():
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#singleton_text_test_concept"
    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts.insert_one({"concept_id": concept_id, "relationships": {}})

    with bypass_access_control():
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text="old one",
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text="old two",
            lang="en-NZ",
        )

        result = upsert_singleton_text_relation(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text="new description",
            lang="en-NZ",
            garbage_collect=True,
        )

    assert result["success"] is True
    assert result["concept_id"] == concept_id
    assert result["predicate"] == "hasDescription"
    assert result["language"] == "en-NZ"
    assert result["kept_relation_id"]
    assert result["replaced_count"] == 2
    assert len(result["replaced_relation_ids"]) == 2

    remaining = list(
        TextRelationsRepository.find(
            {"subject_concept_id": concept_id, "predicate": "hasDescription"},
            projection={"_id": 1},
        )
    )
    assert len(remaining) == 1
