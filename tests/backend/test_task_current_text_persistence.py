"""Continuing tasks retain their current fields after many ordinary edits."""

from unittest.mock import Mock

import mongomock
import pytest
from bson import ObjectId

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from src.backend.integrations.internal_mcp import catalogue
from src.backend.services import task_management_service as tasks
from src.backend.services import text_value_service as texts


@pytest.fixture
def task_store(monkeypatch):
    db = mongomock.MongoClient()["task_current_text"]
    monkeypatch.setattr(ConceptsRepository, "collection", lambda: db.concepts)
    monkeypatch.setattr(TextRelationsRepository, "collection", lambda: db.relations)
    monkeypatch.setattr(TextValuesRepository, "collection", lambda: db.values)
    monkeypatch.setattr(texts, "can_access_concept", lambda _: True)
    monkeypatch.setattr(texts, "filter_accessible_concept_ids", lambda ids: set(ids))
    monkeypatch.setattr(texts, "_emit_text_relation_mutation_event", lambda **_: None)
    monkeypatch.setattr(texts, "_invalidate_stats_for_predicate_change", lambda _: None)
    monkeypatch.setattr(
        texts,
        "_invalidate_workflow_routing_projection_for_text_relation_change",
        lambda **_: None,
    )
    doc = {"concept_id": "#V#continuing_task", "relationships": {}, "metadata": {}}
    db.concepts.insert_one(doc)
    monkeypatch.setattr(tasks, "_get_task_doc", lambda cid: (cid, doc))
    monkeypatch.setattr(
        tasks, "resolve_task_work_product", lambda _: {"status": "missing"}
    )
    monkeypatch.setattr(tasks, "_append_task_history_event", Mock())
    return db


def seed(text, *, predicate="hasNote", lang="en-NZ", task="#V#continuing_task"):
    return texts.upsert_text_for_concept(
        subject_concept_id=task,
        predicate=predicate,
        text=text,
        lang=lang,
    )


def test_current_fields_survive_legacy_overflow_and_repeated_revisions(task_store):
    # Reproduce the old additive store exceeding the task reader's 50 rows.
    old_values = [seed(f"Old observation {i}")["text_value_id"] for i in range(60)]
    seed("Standing research requirements", predicate="hasDescription")
    seed("Conserver cette traduction", lang="fr")
    seed("An independent annotation", predicate="hasContent")
    seed("Another task's note", task="#V#another_task")

    for revision in range(55):
        fields = {
            "notes": f"Observation {revision}",
            "next_checkpoint": f"Inspect source {revision}",
            "progress_signal": f"Progress {revision}",
            "evidence": f"Evidence {revision}",
        }
        result = tasks.update_task_fields("#V#continuing_task", fields=fields)
        receipt = catalogue._task_continuation_readback(
            "#V#continuing_task",
            fields,
            result["task"],
        )
        assert receipt["verified"] is True
        assert all(result["task"][key] == value for key, value in fields.items())
        assert result["task"]["description"] == "Standing research requirements"

    # List and point reads agree; the storage test uses the real text readers.
    listed = tasks._build_task_responses([tasks._get_task_doc("#V#continuing_task")[1]])
    assert listed[0]["notes"] == "Observation 54"
    assert listed[0]["next_checkpoint"] == "Inspect source 54"
    assert (
        task_store.relations.count_documents(
            {"subject_concept_id": "#V#continuing_task"}
        )
        == 7
    )
    assert len(texts.get_texts_for_concept("#V#continuing_task", lang="fr")) == 1
    assert (
        texts.get_texts_for_concept("#V#another_task")[0]["text"]
        == "Another task's note"
    )
    assert (
        task_store.values.count_documents(
            {"_id": {"$in": [ObjectId(value) for value in old_values]}}
        )
        == 60
    )


@pytest.mark.parametrize("field", ["notes", "next_checkpoint", "evidence"])
def test_repeated_value_and_clear_keep_other_fields(task_store, field):
    seed("Independent note", predicate="hasContent")
    for _ in range(2):
        result = tasks.update_task_fields(
            "#V#continuing_task", fields={field: "Current"}
        )
        assert result["task"][field] == "Current"
    assert task_store.relations.count_documents({}) == 2
    cleared = tasks.update_task_fields("#V#continuing_task", fields={field: None})
    assert cleared["task"][field] is None
    assert (
        texts.get_texts_for_concept("#V#continuing_task")[0]["text"]
        == "Independent note"
    )
