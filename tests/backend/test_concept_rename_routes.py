from __future__ import annotations

from flask import Flask

from src.backend.server.routes.concept_routes import concept_bp


def _make_concept_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(concept_bp, url_prefix="/api/concepts")
    return app


def test_concept_rename_route_forbids_non_admin_session(monkeypatch):
    app = _make_concept_app()
    called = {"hit": False}

    def _fake_rename(**_kwargs):
        called["hit"] = True
        return {"success": True}

    monkeypatch.setattr(
        "src.backend.services.concept_rename_service.rename_concept",
        _fake_rename,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#member"
            sess["role_in_org"] = "member"

        response = client.post(
            "/api/concepts/%23V%23old_name/rename",
            json={"new_id": "#V#new_name", "simulate": True},
        )

    assert response.status_code == 403
    assert called["hit"] is False


def test_concept_rename_route_defaults_to_preview_for_admin(monkeypatch):
    app = _make_concept_app()
    captured: dict[str, object] = {}

    def _fake_rename(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "simulate": kwargs["simulate"],
            "old_id": kwargs["old_id"],
            "new_id": "#V#new_name",
            "operations": [{"type": "update_concept_id"}],
        }

    monkeypatch.setattr(
        "src.backend.services.concept_rename_service.rename_concept",
        _fake_rename,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#admin"
            sess["role_in_org"] = "admin"

        response = client.post(
            "/api/concepts/%23V%23old_name/rename",
            json={"new_id": "#V#new_name"},
        )

    assert response.status_code == 200
    payload = response.get_json() or {}
    assert payload["success"] is True
    assert payload["simulate"] is True
    assert captured == {
        "old_id": "#V#old_name",
        "new_id": "#V#new_name",
        "simulate": True,
        "preserve_alias": True,
        "skip_inaccessible": False,
    }


def test_concept_rename_route_allows_owner_execution(monkeypatch):
    app = _make_concept_app()
    captured: dict[str, object] = {}

    def _fake_rename(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "simulate": kwargs["simulate"],
            "executed": not kwargs["simulate"],
            "old_id": kwargs["old_id"],
            "new_id": "#V#new_name",
        }

    monkeypatch.setattr(
        "src.backend.services.concept_rename_service.rename_concept",
        _fake_rename,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#owner"
            sess["role_in_org"] = "owner"

        response = client.post(
            "/api/concepts/%23V%23old_name/rename",
            json={"new_id": "#V#new_name", "simulate": False},
        )

    assert response.status_code == 200
    payload = response.get_json() or {}
    assert payload["executed"] is True
    assert captured["simulate"] is False


def test_concept_rename_route_coerces_string_booleans(monkeypatch):
    app = _make_concept_app()
    captured: dict[str, object] = {}

    def _fake_rename(**kwargs):
        captured.update(kwargs)
        return {"success": True, "simulate": kwargs["simulate"]}

    monkeypatch.setattr(
        "src.backend.services.concept_rename_service.rename_concept",
        _fake_rename,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#admin"
            sess["role_in_org"] = "admin"

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
    assert captured["simulate"] is False
    assert captured["preserve_alias"] is False
    assert captured["skip_inaccessible"] is True
