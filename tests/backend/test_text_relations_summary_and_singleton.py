from datetime import datetime, timedelta, timezone

import pytest
from bson.objectid import ObjectId

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from src.backend.security.access_control import bypass_access_control
from src.backend.services.text_value_service import (
    get_text_relations_summary,
    get_texts_for_concept,
    get_texts_for_concepts,
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


def test_workflow_routing_text_context_update_does_not_invalidate_projection(
    monkeypatch: pytest.MonkeyPatch,
):
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#workflow_context_update_test"
    predicate = "#V#hasWorkflowDiscoveryExemplarsJson"
    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts.insert_one({"concept_id": concept_id, "relationships": {}})
    invalidations: list[str | None] = []
    discovery_cache_clears: list[bool] = []

    monkeypatch.setattr(
        "src.backend.services.workflow_capability_service.invalidate_workflow_capability_index",
        lambda **kwargs: (
            invalidations.append(kwargs.get("reason")) or {"success": True}
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.invalidate_workflow_discovery_executability_caches",
        lambda: discovery_cache_clears.append(True),
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_capability_service.is_authoritative_workflow_concept_id",
        lambda subject_concept_id: subject_concept_id == concept_id,
    )

    text = '{"schema_version":"workflow_discovery_exemplars.v1","keywords":["demo"]}'
    with bypass_access_control():
        created = upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate=predicate,
            text=text,
            lang="en-NZ",
            context={"source": "first"},
        )
        context_only_update = upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate=predicate,
            text=text,
            lang="en-NZ",
            context={"source": "second"},
        )

    assert created["relation_created"] is True
    assert created["context_updated"] is False
    assert context_only_update["relation_created"] is False
    assert context_only_update["context_updated"] is True
    assert context_only_update["text_value_id"] == created["text_value_id"]
    assert invalidations == [
        f"workflow_routing_text_relation_changed:{concept_id}:{predicate}"
    ]
    assert discovery_cache_clears == [True]


def test_get_texts_for_concept_can_return_recent_rows_first():
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#recent_text_test_concept"
    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts.insert_one({"concept_id": concept_id, "relationships": {}})
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer = older + timedelta(days=1)

    with bypass_access_control():
        first = upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text="older",
            lang="en-NZ",
        )
        second = upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text="newer",
            lang="en-NZ",
        )
        TextRelationsRepository.update_one(
            {"_id": ObjectId(first["relation_id"])},
            {"$set": {"created_at": older, "updated_at": older}},
        )
        TextRelationsRepository.update_one(
            {"_id": ObjectId(second["relation_id"])},
            {"$set": {"created_at": newer, "updated_at": newer}},
        )

        rows = get_texts_for_concept(
            concept_id,
            predicate="hasDescription",
            limit=1,
            recent_first=True,
        )

    assert [row["text"] for row in rows] == ["newer"]


def test_get_texts_for_concepts_batches_rows_by_subject():
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    first_concept_id = "#V#batch_text_test_first"
    second_concept_id = "#V#batch_text_test_second"
    concepts.insert_many(
        [
            {"concept_id": first_concept_id, "relationships": {}},
            {"concept_id": second_concept_id, "relationships": {}},
        ]
    )

    with bypass_access_control():
        upsert_text_for_concept(
            subject_concept_id=first_concept_id,
            predicate="hasDescription",
            text="first description",
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=first_concept_id,
            predicate="hasNote",
            text="first note",
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=second_concept_id,
            predicate="hasDescription",
            text="second description",
            lang="en-NZ",
        )

        rows_by_concept = get_texts_for_concepts(
            [first_concept_id, second_concept_id],
            predicate="hasDescription",
            limit_per_concept=5,
            max_time_ms=5000,
        )

    assert {
        row["text"] for row in rows_by_concept[first_concept_id]
    } == {"first description"}
    assert {
        row["text"] for row in rows_by_concept[second_concept_id]
    } == {"second description"}
