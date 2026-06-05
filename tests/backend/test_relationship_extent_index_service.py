import pytest


mongomock = pytest.importorskip("mongomock")


def test_relationship_extent_index_materialises_dynamic_incoming_rows(monkeypatch):
    from src.backend.services import relationship_extent_index_service as service

    client = mongomock.MongoClient()
    coll = client.db.relationship_extent_index
    settings = client.db.application_settings

    monkeypatch.setattr(service, "get_relationship_extent_index_collection", lambda: coll)
    monkeypatch.setattr(service, "get_application_settings_collection", lambda: settings)
    monkeypatch.setattr(
        service,
        "get_relationship_kinds_set",
        lambda: {"is_a_type_of", "has_subtype", "is_an_instance_of", "has_instance"},
    )

    result = service.sync_relationship_extent_index_for_concept_doc(
        {
            "concept_id": "#V#source",
            "relationships": {
                "#V#attended_event": ["#V#target"],
                "is_a_type_of": ["#V#target"],
            },
        }
    )

    assert result["success"] is True
    assert result["inserted"] == 2

    rows, used_index = service.incoming_dynamic_extent_rows_for_target("#V#target")

    assert used_index is True
    assert len(rows) == 1
    assert rows[0]["predicate_id"] == "#V#attended_event"
    assert rows[0]["arg1_value"] == "#V#source"
    assert rows[0]["arg2_value"] == "#V#target"


def test_predicate_structured_extent_uses_relationship_extent_index(monkeypatch):
    from src.backend.server.routes import predicate_routes

    client = mongomock.MongoClient()
    concepts = client.db.concepts
    concepts.insert_many(
        [
            {"concept_id": "#V#source", "name": "Source concept"},
            {"concept_id": "#V#target", "name": "Target concept"},
        ]
    )

    monkeypatch.setattr(predicate_routes, "get_concepts_collection", lambda: concepts)
    monkeypatch.setattr(
        predicate_routes,
        "query_relationship_extent_index",
        lambda **kwargs: (
            [
                {
                    "source_concept_id": "#V#source",
                    "predicate_id": "#V#attended_event",
                    "target_value": "#V#target",
                    "target_index": 0,
                }
            ],
            1,
        ),
    )

    items, total = predicate_routes._query_structured_relations_extent(
        "#V#attended_event",
        subject_type=None,
        object_type=None,
        limit=100,
        offset=0,
        sort_by="updated_at",
        sort_order="desc",
    )

    assert total == 1
    assert items == [
        {
            "subject": "#V#source",
            "subject_name": "Source concept",
            "predicate": "#V#attended_event",
            "object": "#V#target",
            "object_name": "Target concept",
            "source": "structured",
            "updated_at": None,
        }
    ]
