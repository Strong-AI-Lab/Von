"""Regression tests for upsert_text_relation provenance passthrough."""

from __future__ import annotations

from src.backend.integrations.internal_mcp import catalogue


def test_upsert_text_relation_passes_provenance(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_upsert_text_for_concept(**kwargs):
        captured.update(kwargs)
        return {
            "text_value_id": "tv-1",
            "relation_id": "rel-1",
            "relation_created": True,
        }

    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_text_for_concept",
        _fake_upsert_text_for_concept,
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    result = catalogue._upsert_text_relation(
        concept_id="#V#example",
        predicate="hasDescription",
        text="Example text",
        provenance={"source": "unit_test", "turn_id": "turn-123"},
    )

    assert result.get("success") is True
    assert captured.get("subject_concept_id") == "#V#example"
    assert captured.get("predicate") == "hasDescription"
    assert captured.get("provenance") == {"source": "unit_test", "turn_id": "turn-123"}
