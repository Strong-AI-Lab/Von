from __future__ import annotations


def test_rag_list_indexed_supports_vontology_text_relations(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: object(),
    )

    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_sync_service.list_text_relation_index_items",
        lambda **_kwargs: {
            "items": [
                {
                    "relation_id": "r1",
                    "subject_concept_id": "#V#x",
                    "predicate": "hasDescription",
                    "text_value_id": "t1",
                    "lang": "en-NZ",
                    "preview_length": 12,
                    "updated_at": "2025-01-01T00:00:00Z",
                }
            ],
            "total": 1,
            "scanned": 1,
            "limit": 10,
            "offset": 0,
            "scan_limit": 5000,
        },
    )

    result = cat._rag_list_indexed(
        namespace="#V#user@org",
        collection="vontology_text_relations",
        limit=10,
        offset=0,
    )

    assert result["success"] is True
    assert result["collection"] == "vontology_text_relations"
    assert result["provenance"]["item_kind"] == "rag_text_relation_list"

    assert result["items"]
    item = result["items"][0]
    assert item["item_kind"] == "vontology_text_relation"
    assert item["source_system"] == "mongo.text_relations"
    assert item["session_id"] == "r1"


def test_rag_get_item_supports_vontology_text_relations(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: object(),
    )

    class _Doc:
        def __init__(self):
            self.text = (
                "Concept: #V#x\nPredicate: hasDescription\nLanguage: en-NZ\n\nHello"
            )
            self.metadata = {
                "relation_id": "r1",
                "subject_concept_id": "#V#x",
                "predicate": "hasDescription",
                "lang": "en-NZ",
            }

    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_sync_service.get_text_relation_preview",
        lambda **_kwargs: _Doc(),
    )

    result = cat._rag_get_item(
        namespace="#V#user@org",
        collection="vontology_text_relations",
        session_id="r1",
    )

    assert result["success"] is True
    assert result["collection"] == "vontology_text_relations"
    assert result["provenance"]["item_kind"] == "rag_text_relation_item"
    assert "Hello" in result["preview"]
