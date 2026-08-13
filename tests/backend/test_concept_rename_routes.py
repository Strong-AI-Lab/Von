from __future__ import annotations

from flask import Flask

from src.backend.server.routes.concept_routes import concept_bp


def _make_concept_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(concept_bp, url_prefix="/api/concepts")
    return app


def test_concept_rename_route_returns_semantic_authority_denial(monkeypatch):
    app = _make_concept_app()
    called = {"hit": False}

    def _fake_rename(**_kwargs):
        called["hit"] = True
        return {"success": True}

    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._execute_governed_http_mutation",
        lambda **_kwargs: {
            "success": False,
            "error_code": "organisation_ontology_admin_authority_required",
            "authority_decision": {"allowed": False},
        },
    )
    monkeypatch.setattr("src.backend.services.concept_rename_service.rename_concept", _fake_rename)

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#member"
            sess["role_in_org"] = "admin"  # Deliberately not semantic authority.

        response = client.post(
            "/api/concepts/%23V%23old_name/rename",
            json={"new_id": "#V#new_name", "simulate": True},
        )

    assert response.status_code == 403
    assert called["hit"] is False


def test_concept_rename_route_uses_governed_semantic_command(monkeypatch):
    app = _make_concept_app()
    captured: dict[str, object] = {}

    def _fake_governed(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "simulate": kwargs["arguments"]["simulate"],
            "new_id": kwargs["arguments"]["new_id"],
            "authority_decision": {"allowed": True},
        }

    # The transport route is tested against a result already authorised by the
    # shared command service; session role labels no longer decide authority.
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._execute_governed_http_mutation",
        _fake_governed,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#semantic_admin"

        response = client.post(
            "/api/concepts/%23V%23old_name/rename",
            json={"new_id": "#V#new_name"},
        )

    assert response.status_code == 200
    payload = response.get_json() or {}
    assert payload["success"] is True
    assert payload["simulate"] is True
    assert captured["arguments"] == {
        "concept_id": "#V#old_name",
        "new_id": "#V#new_name",
        "simulate": True,
        "request_id": None,
    }


def test_concept_rename_route_coerces_string_booleans_for_governed_command(monkeypatch):
    app = _make_concept_app()
    captured: dict[str, object] = {}

    def _fake_governed(**kwargs):
        captured.update(kwargs)
        return {"success": True, "simulate": kwargs["arguments"]["simulate"]}

    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._execute_governed_http_mutation",
        _fake_governed,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#semantic_admin"

        response = client.post(
            "/api/concepts/%23V%23old_name/rename",
            json={
                "new_id": "#V#new_name",
                "simulate": "false",
                "preserve_alias": "false",
                "skip_inaccessible": "true",
            },
        )

    assert response.status_code == 200
    assert captured["arguments"] == {
        "concept_id": "#V#old_name",
        "new_id": "#V#new_name",
        "simulate": False,
        "request_id": None,
    }
    assert captured["preview"] is False


def test_concept_rename_route_maps_canonical_lookup_failure_to_typed_not_found(monkeypatch):
    app = _make_concept_app()

    def _raise_lookup_error(**_kwargs):
        raise LookupError("concept store unavailable")

    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service.execute_governed_ontology_method",
        _raise_lookup_error,
    )

    response = app.test_client().post(
        "/api/concepts/%23V%23old_name/rename",
        json={"new_id": "#V#new_name"},
    )

    assert response.status_code == 404
    assert response.get_json()["error_code"] == "ontology_mutation_target_not_found"


def test_concept_rename_route_maps_store_failure_to_typed_unavailable(monkeypatch):
    app = _make_concept_app()

    def _raise_store_error(**_kwargs):
        raise ConnectionError("ontology store unavailable")

    monkeypatch.setattr(
        "src.backend.services.ontology_mutation_command_service.execute_governed_ontology_method",
        _raise_store_error,
    )

    response = app.test_client().post(
        "/api/concepts/%23V%23old_name/rename",
        json={"new_id": "#V#new_name"},
    )

    assert response.status_code == 503
    assert response.get_json()["error_code"] == "ontology_mutation_store_unavailable"
