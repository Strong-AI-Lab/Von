from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import mongomock
import pytest

from src.backend.services import concept_merge_service as merge_service
from src.backend.services import ontology_publication_authority_service as authority


@pytest.fixture
def merge_store(monkeypatch: pytest.MonkeyPatch):
    database = mongomock.MongoClient()["concept_merge"]
    concepts = database["concepts"]
    text_relations = database["text_relations"]
    text_values = database["text_values"]

    from src.backend.db.repositories import concepts_repository
    from src.backend.db.repositories import text_value_repository

    monkeypatch.setattr(
        concepts_repository,
        "get_concepts_collection",
        lambda: concepts,
    )
    monkeypatch.setattr(
        text_value_repository,
        "get_text_relations_collection",
        lambda: text_relations,
    )
    monkeypatch.setattr(
        text_value_repository,
        "get_text_values_collection",
        lambda: text_values,
    )
    monkeypatch.setattr(merge_service, "can_access_concept", lambda _item: True)
    return concepts, text_relations, text_values


def _concept(
    concept_id: str,
    *,
    relationships: dict[str, Any] | None = None,
    guid: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    return {
        "concept_id": concept_id,
        "guid": guid or f"guid:{concept_id}",
        "created_at": now,
        "updated_at": now,
        "embedding_status": "ready",
        "attributes": attributes or {},
        "system_tags": [],
        "user_tags": [],
        "relationships": relationships or {},
    }


def _insert_text_relation(
    text_relations,
    text_values,
    *,
    relation_id: str,
    subject_id: str,
    predicate: str,
    text_id: str,
    text: str,
    context: dict[str, Any] | None = None,
) -> None:
    text_values.update_one(
        {"_id": text_id},
        {"$setOnInsert": {"_id": text_id, "text": text, "lang": "en-NZ"}},
        upsert=True,
    )
    text_relations.insert_one(
        {
            "_id": relation_id,
            "subject_concept_id": subject_id,
            "predicate": predicate,
            "object_text_id": text_id,
            "context": context or {},
        }
    )


def _allowed_decision(intent: authority.OntologyMutationIntent):
    return authority.OntologyAuthorityDecision(
        allowed=True,
        decision_id="decision:merge",
        reason_code="semantic_ontology_authority_verified",
        message="allowed",
        actor_concept_id="#V#admin",
        organisation_concept_id=None,
        intent_fingerprint=intent.fingerprint,
    )


def test_merge_preview_reports_exact_text_actions(merge_store) -> None:
    concepts, text_relations, text_values = merge_store
    source_id = "#V#A"
    target_id = "#V#a"
    concepts.insert_many([_concept(source_id), _concept(target_id)])
    _insert_text_relation(
        text_relations,
        text_values,
        relation_id="r1",
        subject_id=source_id,
        predicate="hasNote",
        text_id="t1",
        text="note",
    )
    _insert_text_relation(
        text_relations,
        text_values,
        relation_id="r2",
        subject_id=source_id,
        predicate="hasDescription",
        text_id="t2",
        text="description",
    )
    _insert_text_relation(
        text_relations,
        text_values,
        relation_id="r3",
        subject_id=target_id,
        predicate="hasNote",
        text_id="t1",
        text="note",
    )

    report = merge_service.merge_concepts(source_id, target_id, simulate=True)

    assert report["success"] is True
    assert report["simulate"] is True
    migrate = next(
        item
        for item in report["operations"]
        if item["type"] == "migrate_text_relations"
    )
    assert migrate["move_count"] == 1
    assert migrate["duplicate_count"] == 1
    assert report["plan_sha256"]


def test_direct_merge_execution_fails_before_any_write(merge_store) -> None:
    concepts, _text_relations, _text_values = merge_store
    source_id = "#V#A"
    target_id = "#V#a"
    concepts.insert_many([_concept(source_id), _concept(target_id)])

    result = merge_service.merge_concepts(source_id, target_id, simulate=False)

    assert result["success"] is False
    assert result["changed"] is False
    assert result["error_code"] == "ontology_identity_consolidation_authority_required"
    assert concepts.find_one({"concept_id": source_id}) is not None


def test_exact_authorised_merge_moves_graph_and_text_and_deletes_source(
    merge_store,
) -> None:
    concepts, text_relations, text_values = merge_store
    source_id = "#V#A"
    target_id = "#V#a"
    source = _concept(
        source_id,
        relationships={
            "related_to": ["#V#Else"],
            "#V#specific_to_user": ["#V#admin"],
        },
        guid="source-guid",
        attributes={"source_only": True},
    )
    target = _concept(
        target_id,
        relationships={"#V#specific_to_user": ["#V#admin"]},
        guid="target-guid",
        attributes={"target_only": True},
    )
    other = _concept(
        "#V#Other",
        relationships={"related_to": [source_id]},
    )
    concepts.insert_many([source, target, other, _concept("#V#Else")])
    _insert_text_relation(
        text_relations,
        text_values,
        relation_id="name-source-id",
        subject_id=source_id,
        predicate="hasName",
        text_id="text-source-id",
        text=source_id,
        context={"name_type": "CODE"},
    )
    _insert_text_relation(
        text_relations,
        text_values,
        relation_id="name-source-guid",
        subject_id=source_id,
        predicate="hasName",
        text_id="text-source-guid",
        text="source-guid",
        context={"name_type": "CODE"},
    )
    _insert_text_relation(
        text_relations,
        text_values,
        relation_id="note-source",
        subject_id=source_id,
        predicate="hasNote",
        text_id="text-note",
        text="source note",
    )

    plan = merge_service.build_concept_merge_plan(source_id, target_id)
    intent = authority.OntologyMutationIntent(
        operation="concept.merge",
        publication_context=authority.PublicationContext.user("#V#admin"),
        target_concept_ids=tuple(plan["affected_concept_ids"]),
        tool_name="merge_concepts",
        delta={"merge_plan_sha256": plan["plan_sha256"]},
    )
    with authority.bind_authorised_ontology_mutation(
        _allowed_decision(intent),
        intent,
    ):
        result = merge_service.merge_concepts(
            source_id,
            target_id,
            simulate=False,
            exact_plan=plan,
        )

    assert result["success"] is True
    assert result["changed"] is True
    assert concepts.find_one({"concept_id": source_id}) is None
    merged_target = concepts.find_one({"concept_id": target_id})
    assert merged_target["relationships"]["#V#specific_to_user"] == ["#V#admin"]
    assert merged_target["relationships"]["related_to"] == ["#V#Else"]
    assert merged_target["attributes"] == {
        "source_only": True,
        "target_only": True,
    }
    assert concepts.find_one({"concept_id": "#V#Other"})["relationships"] == {
        "related_to": [target_id]
    }
    assert text_relations.count_documents({"subject_concept_id": source_id}) == 0
    assert text_relations.count_documents({"subject_concept_id": target_id}) == 3
    read_back = merge_service.read_concept_merge_postcondition(plan)
    assert read_back["matches_exact_plan"] is True


def test_merge_plan_rejects_authority_role_transfer(merge_store) -> None:
    concepts, text_relations, text_values = merge_store
    source_id = "#V#admin_duplicate"
    target_id = "#V#admin"
    concepts.insert_many([_concept(source_id), _concept(target_id)])
    _insert_text_relation(
        text_relations,
        text_values,
        relation_id="role",
        subject_id=source_id,
        predicate="#V#has_ontology_authority_role",
        text_id="role-text",
        text="global_ontology_administrator",
    )

    result = merge_service.merge_concepts(source_id, target_id, simulate=True)

    assert result["success"] is False
    assert result["error_code"] == "identity_consolidation_authority_lifecycle_required"


def test_merge_plan_fails_non_disclosing_when_hidden_reference_would_change(
    merge_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    concepts, _text_relations, _text_values = merge_store
    source_id = "#V#A"
    target_id = "#V#a"
    hidden_id = "#V#hidden"
    concepts.insert_many(
        [
            _concept(source_id),
            _concept(target_id),
            _concept(hidden_id, relationships={"related_to": [source_id]}),
        ]
    )
    monkeypatch.setattr(
        merge_service,
        "can_access_concept",
        lambda concept_id: concept_id != hidden_id,
    )

    result = merge_service.merge_concepts(source_id, target_id, simulate=True)

    assert result["success"] is False
    assert result["error_code"] == "identity_consolidation_inaccessible_affected_state"
    assert hidden_id not in str(result)


def test_merge_plan_enumerates_every_incoming_reference_beyond_preview_limits(
    merge_store,
) -> None:
    concepts, _text_relations, _text_values = merge_store
    source_id = "#V#A"
    target_id = "#V#a"
    incoming_ids = [f"#V#incoming_{index:03d}" for index in range(75)]
    concepts.insert_many(
        [
            _concept(source_id),
            _concept(target_id),
            *[
                _concept(concept_id, relationships={"related_to": [source_id]})
                for concept_id in incoming_ids
            ],
        ]
    )

    plan = merge_service.build_concept_merge_plan(source_id, target_id)

    assert set(incoming_ids).issubset(plan["affected_concept_ids"])
    assert len(plan["affected_concept_ids"]) == len(incoming_ids) + 2
    assert len(plan["concept_steps"]) == len(incoming_ids) + 1


def test_merge_plan_rejects_implicit_target_name_context_rewrite(merge_store) -> None:
    concepts, text_relations, text_values = merge_store
    source_id = "#V#A"
    target_id = "#V#a"
    concepts.insert_many([_concept(source_id), _concept(target_id)])
    _insert_text_relation(
        text_relations,
        text_values,
        relation_id="target-nl-alias",
        subject_id=target_id,
        predicate="hasName",
        text_id="source-id-text",
        text=source_id,
        context={"name_type": "NL"},
    )

    result = merge_service.merge_concepts(source_id, target_id, simulate=True)

    assert result["success"] is False
    assert result["error_code"] == "identity_consolidation_alias_context_conflict"
