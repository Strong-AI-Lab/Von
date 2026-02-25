"""HTTP contract tests for relationship removal preview/bulk/undo routes."""

from __future__ import annotations

from flask import Flask

from src.backend.server.routes.vontology_routes import vontology_bp


def _build_client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test-secret"
    app.register_blueprint(vontology_bp, url_prefix="/api/vontology")
    return app.test_client()


def test_preview_remove_relationship_route_contract(monkeypatch):
    client = _build_client()

    monkeypatch.setattr(
        "src.backend.services.relationship_removal_service.preview_remove_relationship",
        lambda **_kwargs: {
            "success": True,
            "status": "ok",
            "dry_run": True,
            "correlation_id": "corr-1",
            "source_id": "#V#source",
            "predicate": "is_a_type_of",
            "target": "#V#target",
            "relation_id": "rel:abc.def.ghi",
            "impact": {"warnings": []},
            "warnings": [],
        },
    )

    response = client.post(
        "/api/vontology/relationships/remove/preview",
        json={"source_id": "#V#source", "kind": "typeOf", "target_id": "#V#target"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["dry_run"] is True
    assert payload["status"] == "ok"
    assert payload["relation_id"] == "rel:abc.def.ghi"


def test_bulk_remove_relationship_route_contract(monkeypatch):
    client = _build_client()

    monkeypatch.setattr(
        "src.backend.services.relationship_removal_service.remove_relationships_bulk",
        lambda **_kwargs: {
            "success": True,
            "status": "partial",
            "correlation_id": "corr-2",
            "summary": {
                "total_requested": 2,
                "total_valid": 1,
                "removed_count": 1,
                "already_absent_count": 0,
                "error_count": 1,
            },
            "results": [],
            "undo_token": "undo:corr-2",
        },
    )

    response = client.post(
        "/api/vontology/relationships/remove/bulk",
        json={"relation_ids": ["rel:one.two.three"], "confirmed": True},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["status"] == "partial"
    assert payload["summary"]["removed_count"] == 1
    assert payload["undo_token"] == "undo:corr-2"


def test_undo_remove_relationship_route_contract(monkeypatch):
    client = _build_client()

    monkeypatch.setattr(
        "src.backend.services.relationship_removal_service.undo_relationship_removal",
        lambda **_kwargs: {
            "success": True,
            "status": "ok",
            "undo_token": "undo:corr-3",
            "restored_count": 1,
            "already_restored_count": 0,
            "error_count": 0,
            "errors": [],
        },
    )

    response = client.post(
        "/api/vontology/relationships/remove/undo",
        json={"undo_token": "undo:corr-3"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["status"] == "ok"
    assert payload["restored_count"] == 1


def test_remove_relationship_route_uses_canonical_service(monkeypatch):
    client = _build_client()

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *args, **kwargs: {"concept_id": "#V#source", "relationships": {}},
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_removal_service.remove_relationship",
        lambda **_kwargs: {
            "success": True,
            "status": "ok",
            "source_id": "#V#source",
            "predicate": "is_a_type_of",
            "target": "#V#target",
            "relation_id": "rel:abc.def.ghi",
            "removed": True,
            "mode_returned": "soft_delete",
            "cascade_policy": "warn",
            "correlation_id": "corr-4",
        },
    )

    response = client.post(
        "/api/vontology/relationships/remove",
        json={"source_id": "#V#source", "kind": "is_a_type_of", "target_id": "#V#target"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["status"] == "ok"
    assert payload["relation_id"] == "rel:abc.def.ghi"
