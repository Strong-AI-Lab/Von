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
                    "index_item_id": "text_relation:r1",
                    "row_kind": "base_text_relation",
                    "relation_id": "r1",
                    "assertion_id": None,
                    "subject_concept_id": "#V#x",
                    "predicate": "hasDescription",
                    "text_value_id": "t1",
                    "lang": "en-NZ",
                    "preview_length": 12,
                    "updated_at": "2025-01-01T00:00:00Z",
                },
                {
                    "index_item_id": "scoped_assertion:ska_1",
                    "row_kind": "scoped_assertion",
                    "relation_id": None,
                    "assertion_id": "ska_1",
                    "subject_concept_id": "#V#x",
                    "predicate": "hasNote",
                    "text_value_id": None,
                    "lang": "en-NZ",
                    "preview_length": 10,
                    "updated_at": "2025-01-02T00:00:00Z",
                }
            ],
            "total": 2,
            "scanned": 2,
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
    assert item["index_item_id"] == "text_relation:r1"
    assert item["row_kind"] == "base_text_relation"

    scoped_item = result["items"][1]
    assert scoped_item["session_id"] == "ska_1"
    assert scoped_item["index_item_id"] == "scoped_assertion:ska_1"
    assert scoped_item["relation_id"] is None
    assert scoped_item["assertion_id"] == "ska_1"
    assert scoped_item["row_kind"] == "scoped_assertion"
    assert scoped_item["source_system"] == "mongo.scoped_knowledge_assertions"


def test_rag_get_item_supports_vontology_text_relations(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: object(),
    )

    class _Doc:
        def __init__(self):
            self.doc_id = "text_relation:r1"
            self.text = (
                "Concept: #V#x\nPredicate: hasDescription\nLanguage: en-NZ\n\nHello"
            )
            self.metadata = {
                "row_kind": "base_text_relation",
                "relation_id": "r1",
                "assertion_id": None,
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
    assert result["index_item_id"] == "text_relation:r1"
    assert result["row_kind"] == "base_text_relation"
    assert result["assertion_id"] is None
