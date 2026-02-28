from datetime import datetime, timezone
from types import SimpleNamespace

from bson import ObjectId

from src.backend.services.description_metadata_service import (
    extract_inline_description_metadata,
)
from src.backend.services import concept_service, text_value_service


def test_extract_inline_description_metadata_strips_header_block():
    raw = (
        "Source: relation_elicitation\n"
        "Confidence: 78%\n"
        "Timestamp: 2026-03-01T00:00:00Z\n\n"
        "This is the canonical description body."
    )
    cleaned, metadata = extract_inline_description_metadata(raw)
    assert cleaned == "This is the canonical description body."
    assert metadata["source"] == "relation_elicitation"
    assert metadata["confidence_score"] == 0.78
    assert metadata["timestamp"] == "2026-03-01T00:00:00Z"
    assert metadata["migrated_from_inline_header"] is True


def test_update_concept_description_preserves_existing_structured_metadata(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        concept_service.ConceptsRepository,
        "find_one",
        lambda *args, **kwargs: {"_id": ObjectId()},
    )
    monkeypatch.setattr(
        concept_service,
        "get_texts_for_concept",
        lambda *args, **kwargs: [
            {
                "context": {"confidence_score": 0.91, "parent_concept_id": "#V#workflow"},
                "provenance": {"source": "llm_generation", "attribution": "#V#agent"},
            }
        ],
    )

    def _fake_upsert(**kwargs):
        captured.update(kwargs)
        return {"text_value_id": "tv1", "relation_id": "rel1"}

    monkeypatch.setattr(concept_service, "upsert_text_for_concept", _fake_upsert)
    monkeypatch.setattr(
        concept_service.TextRelationsRepository,
        "delete_many",
        lambda *args, **kwargs: SimpleNamespace(deleted_count=0),
    )
    monkeypatch.setattr(
        concept_service.ConceptsRepository,
        "update_one",
        lambda *args, **kwargs: None,
    )

    ok = concept_service.update_concept_description(
        "#V#desc_test",
        "Source: inline_legacy\nConfidence: 20%\n\nUpdated description text.",
    )
    assert ok is True
    assert captured["text"] == "Updated description text."
    assert captured["provenance"]["source"] == "llm_generation"
    assert captured["context"]["confidence_score"] == 0.91
    assert captured["context"]["inline_metadata_migrated"] is True


def test_get_texts_for_concept_returns_provenance_and_timestamps(monkeypatch):
    relation_id = ObjectId()
    text_value_id = ObjectId()
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(text_value_service, "can_access_concept", lambda *_: True)
    monkeypatch.setattr(
        text_value_service.TextRelationsRepository,
        "find",
        lambda *args, **kwargs: [
            {
                "_id": relation_id,
                "predicate": "hasDescription",
                "object_text_id": text_value_id,
                "context": {"confidence_score": 0.66},
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    monkeypatch.setattr(
        text_value_service.TextValuesRepository,
        "find",
        lambda *args, **kwargs: [
            {
                "_id": text_value_id,
                "text": "Body",
                "lang": "en-NZ",
                "provenance": {"source": "unit_test"},
                "created_at": now,
                "updated_at": now,
            }
        ],
    )

    rows = text_value_service.get_texts_for_concept("#V#desc_test", predicate="hasDescription")
    assert len(rows) == 1
    first = rows[0]
    assert first["provenance"]["source"] == "unit_test"
    assert first["relation_updated_at"] == now.isoformat()
    assert first["text_value_updated_at"] == now.isoformat()
