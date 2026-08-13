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


def test_bulk_remove_relationship_route_rejects_invalid_selector_before_effect(
    monkeypatch,
):
    client = _build_client()
    calls = []

    monkeypatch.setattr(
        "src.backend.services.relationship_removal_service.remove_relationships_bulk",
        lambda **kwargs: calls.append(kwargs),
    )

    response = client.post(
        "/api/vontology/relationships/remove/bulk",
        json={"relation_ids": ["rel:one.two.three"], "confirmed": True},
    )
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["error_code"] == "invalid_relationship_relation_id"
    assert payload["effect_status"] == "not_started"
    assert payload["changed"] is False
    assert calls == []


def test_undo_remove_relationship_route_fails_closed_until_governed(monkeypatch):
    client = _build_client()
    calls = []

    monkeypatch.setattr(
        "src.backend.services.relationship_removal_service.undo_relationship_removal",
        lambda **kwargs: calls.append(kwargs),
    )

    response = client.post(
        "/api/vontology/relationships/remove/undo",
        json={"undo_token": "undo:corr-3"},
    )
    assert response.status_code == 410
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["error_code"] == "governed_relationship_restore_required"
    assert payload["effect_status"] == "not_started"
    assert payload["mutation_outcome"] == "not_started"
    assert calls == []


def test_sessionless_remove_relationship_route_fails_before_canonical_service(
    monkeypatch,
):
    client = _build_client()
    calls = []

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *args, **kwargs: {"concept_id": "#V#source", "relationships": {}},
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_removal_service.remove_relationship",
        lambda **kwargs: calls.append(kwargs),
    )

    response = client.post(
        "/api/vontology/relationships/remove",
        json={"source_id": "#V#source", "kind": "is_a_type_of", "target_id": "#V#target"},
    )
    assert response.status_code == 403
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["error_code"] == "authenticated_actor_context_required"
    assert payload["effect_status"] == "not_started"
    assert payload["mutation_outcome"] == "not_started"
    assert payload["changed"] is False
    assert calls == []
