from __future__ import annotations

from flask import Flask

from src.backend.server.routes.vontology_routes import vontology_bp
from src.backend.vontology.utils_vontology import build_pure_instance_query


def _create_client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test-secret"
    app.register_blueprint(vontology_bp, url_prefix="/vontology/api/vontology")
    return app.test_client()


def _matches_pure_instance(doc: dict, type_ids: list[str]) -> bool:
    relationships = doc.get("relationships") or {}

    raw_instance_of = relationships.get("is_an_instance_of")
    if isinstance(raw_instance_of, str):
        instance_of = [raw_instance_of]
    elif isinstance(raw_instance_of, list):
        instance_of = [value for value in raw_instance_of if isinstance(value, str)]
    else:
        instance_of = []

    raw_type_of = relationships.get("is_a_type_of")
    if isinstance(raw_type_of, str):
        type_of = [raw_type_of]
    elif isinstance(raw_type_of, list):
        type_of = [value for value in raw_type_of if isinstance(value, str)]
    else:
        type_of = []

    return any(value in type_ids for value in instance_of) and not any(
        value.strip() for value in type_of
    )


def test_entity_counts_route_uses_direct_pure_instance_stats(monkeypatch):
    client = _create_client()

    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.find",
        lambda *_args, **_kwargs: [
            {"concept_id": "#V#animal", "name": "Animal"},
            {"concept_id": "#V#mammal", "name": "Mammal"},
            {"concept_id": "#V#predicate", "name": "Predicate"},
            {"concept_id": "#V#alice", "name": "Alice"},
        ],
    )

    def _fake_stats(ids, **_kwargs):
        assert ids == ["#V#animal", "#V#mammal", "#V#predicate", "#V#alice"]
        return {
            "stats_status": "available",
            "concept_stats": {
                "#V#animal": {
                    "kind": "type",
                    "direct_instance_count": 1,
                    "direct_pure_instance_count": 0,
                },
                "#V#mammal": {
                    "kind": "type",
                    "direct_instance_count": 2,
                    "direct_pure_instance_count": 1,
                },
                "#V#predicate": {
                    "kind": "type",
                    "direct_instance_count": 1,
                    "direct_pure_instance_count": 1,
                },
                "#V#alice": {
                    "kind": "individual",
                },
            },
        }

    monkeypatch.setattr(
        "src.backend.services.vontology_concept_stats_service.get_vontology_concept_stats",
        _fake_stats,
    )

    resp = client.get("/vontology/api/vontology/entity_counts")
    assert resp.status_code == 200

    payload = resp.get_json()
    counts = payload["entity_counts"]
    assert counts["#V#animal"]["entity_count"] == 0
    assert counts["#V#animal"]["has_entities"] is False
    assert counts["#V#mammal"]["entity_count"] == 1
    assert counts["#V#mammal"]["has_entities"] is True
    assert counts["#V#predicate"]["entity_count"] == 1
    assert counts["#V#alice"]["entity_count"] == 0


def test_instances_route_uses_structural_pure_instance_filter(monkeypatch):
    client = _create_client()
    docs = [
        {
            "concept_id": "#V#alice",
            "name": "Alice",
            "notes": "direct pure instance",
            "metadata": {"concept_type": "collection"},
            "relationships": {"is_an_instance_of": "#V#mammal"},
        },
        {
            "concept_id": "#V#chimp",
            "name": "Chimp",
            "notes": "subtype pure instance",
            "relationships": {"is_an_instance_of": ["#V#primate"]},
        },
        {
            "concept_id": "#V#mammal_kind",
            "name": "Mammal Kind",
            "notes": "higher-order type",
            "relationships": {
                "is_an_instance_of": ["#V#mammal"],
                "is_a_type_of": ["#V#animal"],
            },
        },
        {
            "concept_id": "#V#has_pet",
            "name": "Has Pet",
            "notes": "predicate instance",
            "relationships": {"is_an_instance_of": ["#V#predicate"]},
        },
    ]

    def _fake_find(query, _projection, sort=None):
        assert query == build_pure_instance_query(
            instance_of_any=["#V#mammal", "#V#primate"]
        )
        assert sort == [("name", 1)]
        return [
            doc
            for doc in docs
            if _matches_pure_instance(doc, ["#V#mammal", "#V#primate"])
        ]

    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.get_vontology_node_and_descendant_ids",
        lambda _node_id: ["#V#mammal", "#V#primate"],
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.find",
        _fake_find,
    )

    resp = client.get(
        "/vontology/api/vontology/instances?node_id=%23V%23mammal&include_subtypes=true"
    )
    assert resp.status_code == 200

    payload = resp.get_json()
    ids = [item["id"] for item in payload["instances"]]
    assert ids == ["#V#alice", "#V#chimp"]
    assert "#V#mammal_kind" not in ids
