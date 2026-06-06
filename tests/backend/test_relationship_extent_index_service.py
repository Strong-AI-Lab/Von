import pytest
from flask import Flask

mongomock = pytest.importorskip("mongomock")


def test_relationship_extent_index_materialises_dynamic_incoming_rows(monkeypatch):
    from src.backend.services import relationship_extent_index_service as service

    client = mongomock.MongoClient()
    coll = client.db.relationship_extent_index
    settings = client.db.application_settings

    monkeypatch.setattr(
        service, "get_relationship_extent_index_collection", lambda: coll
    )
    monkeypatch.setattr(
        service, "get_application_settings_collection", lambda: settings
    )
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


def test_incoming_extent_page_filters_private_rows_across_batches(monkeypatch):
    from src.backend.security import access_control
    from src.backend.services import relationship_extent_index_service as service

    client = mongomock.MongoClient()
    index_coll = client.db.relationship_extent_index
    concepts = client.db.concepts
    monkeypatch.setattr(
        service, "get_relationship_extent_index_collection", lambda: index_coll
    )
    monkeypatch.setattr(service, "relationship_extent_index_ready", lambda: True)
    monkeypatch.setattr(access_control, "get_concepts_collection", lambda: concepts)

    for idx, source_id in enumerate(
        [
            "#V#a_private_0",
            "#V#b_private_1",
            "#V#c_private_2",
            "#V#z_public_1",
            "#V#z_public_2",
        ]
    ):
        index_coll.insert_one(
            {
                "schema_version": service.RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
                "relation_id": f"r{idx}",
                "source_concept_id": source_id,
                "predicate_id": "#V#mentions",
                "target_value": "#V#target",
                "target_index": idx,
                "arg2_index": 2,
            }
        )
    concepts.insert_many(
        [
            {
                "concept_id": source_id,
                "relationships": {"specific_to_user": ["#V#other_user"]},
            }
            for source_id in ("#V#a_private_0", "#V#b_private_1", "#V#c_private_2")
        ]
        + [
            {"concept_id": "#V#z_public_1", "relationships": {}},
            {"concept_id": "#V#z_public_2", "relationships": {}},
        ]
    )

    app = Flask(__name__)
    app.secret_key = "test"
    with app.test_request_context("/vontology/api/vontology/relationships/extent"):
        rows, used_index, diagnostics = (
            service.incoming_dynamic_extent_rows_page_for_target(
                "#V#target",
                requested_predicate="#V#mentions",
                visible_limit=2,
                batch_size=2,
                max_index_rows_scanned=10,
                time_budget_ms=10000,
            )
        )

    assert used_index is True
    assert [row["arg1_value"] for row in rows] == ["#V#z_public_1", "#V#z_public_2"]
    assert diagnostics["index_batches"] == 3
    assert diagnostics["index_rows_scanned"] == 5
    assert diagnostics["rows_filtered_by_access"] == 3
    assert diagnostics["complete"] is True


def test_incoming_extent_page_stops_after_has_more_evidence(monkeypatch):
    from src.backend.security import access_control
    from src.backend.services import relationship_extent_index_service as service

    client = mongomock.MongoClient()
    index_coll = client.db.relationship_extent_index
    concepts = client.db.concepts
    monkeypatch.setattr(
        service, "get_relationship_extent_index_collection", lambda: index_coll
    )
    monkeypatch.setattr(service, "relationship_extent_index_ready", lambda: True)
    monkeypatch.setattr(access_control, "get_concepts_collection", lambda: concepts)

    for idx in range(5):
        source_id = f"#V#public_{idx}"
        index_coll.insert_one(
            {
                "schema_version": service.RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
                "relation_id": f"r{idx}",
                "source_concept_id": source_id,
                "predicate_id": "#V#mentions",
                "target_value": "#V#target",
                "target_index": idx,
                "arg2_index": 2,
            }
        )
        concepts.insert_one({"concept_id": source_id, "relationships": {}})

    with access_control.override_current_actor(user_concept_id="#V#viewer"):
        rows, used_index, diagnostics = (
            service.incoming_dynamic_extent_rows_page_for_target(
                "#V#target",
                requested_predicate="#V#mentions",
                visible_limit=2,
                batch_size=2,
                max_index_rows_scanned=10,
                time_budget_ms=10000,
            )
        )

    assert used_index is True
    assert len(rows) == 2
    assert diagnostics["has_more"] is True
    assert diagnostics["complete"] is False
    assert diagnostics["index_rows_scanned"] == 4


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
