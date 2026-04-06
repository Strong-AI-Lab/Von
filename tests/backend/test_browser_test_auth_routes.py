from __future__ import annotations

from flask import Flask, session
import pytest

import src.backend.server.routes.auth_routes as auth_routes


@pytest.fixture
def app_client():
    app = Flask(__name__)
    app.secret_key = "browser-test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(auth_routes.auth_bp, url_prefix="/von")

    with app.test_client() as client:
        yield app, client


def test_auth_status_includes_browser_test_mode_descriptor(monkeypatch, app_client):
    _, client = app_client

    import src.backend.services.browser_test_auth_service as service

    monkeypatch.setattr(
        service,
        "describe_browser_test_mode",
        lambda: {
            "configured": True,
            "available": True,
            "display_name": "Zhan von Witbrock",
            "email": "zhanvonwitbrock@gmail.com",
            "fixture_id": "browser_user_view.v1",
        },
    )

    response = client.get("/von/api/auth/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["authenticated"] is False
    assert payload["browser_test_mode"]["available"] is True
    assert payload["browser_test_mode"]["fixture_id"] == "browser_user_view.v1"


def test_browser_test_login_rejects_when_unavailable(monkeypatch, app_client):
    _, client = app_client

    import src.backend.services.browser_test_auth_service as service

    monkeypatch.setattr(
        service,
        "browser_test_auth_allowed_for_request",
        lambda: (False, "Browser-test auth is disabled"),
    )
    monkeypatch.setattr(
        service,
        "describe_browser_test_mode",
        lambda: {"configured": False, "available": False},
    )

    response = client.post("/von/api/auth/browser-test-login", json={})

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["success"] is False
    assert "disabled" in payload["error"].lower()


def test_browser_test_login_sets_browser_test_session(monkeypatch, app_client):
    _, client = app_client

    import src.backend.services.browser_test_auth_service as service

    monkeypatch.setattr(
        service,
        "browser_test_auth_allowed_for_request",
        lambda: (True, None),
    )
    monkeypatch.setattr(
        service,
        "describe_browser_test_mode",
        lambda: {
            "configured": True,
            "available": True,
            "display_name": "Zhan von Witbrock",
            "email": "zhanvonwitbrock@gmail.com",
            "fixture_id": "browser_user_view.v1",
        },
    )

    def _fake_login(window_session_id=None):
        session["user_email"] = "zhanvonwitbrock@gmail.com"
        session["google_user_info"] = {
            "name": "Zhan von Witbrock",
            "email": "zhanvonwitbrock@gmail.com",
        }
        session["user_concept_id"] = "#V#zhan_von_witbrock"
        session["auth_provider"] = "browser_test_fixture"
        session["browser_test_fixture_id"] = "browser_user_view.v1"
        return {
            "organisation": {
                "concept_id": "#V#university_of_auckland_strong_ai_lab",
                "name": "University Of Auckland Strong AI Lab",
                "role": "member",
            },
            "namespace": "#V#zhan_von_witbrock@university_of_auckland_strong_ai_lab",
            "window_session_id": window_session_id,
            "active_chat_session_id": "browser-fixture-messages-acceptance",
            "fixture": {
                "fixture_id": "browser_user_view.v1",
                "messages": {"total": 3, "created": 3, "reused": 0},
            },
        }

    monkeypatch.setattr(service, "login_browser_test_user", _fake_login)

    response = client.post(
        "/von/api/auth/browser-test-login",
        headers={"X-Von-Window-Session": "ws_browser_test"},
        json={},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["authenticated"] is True
    assert payload["auth_provider"] == "browser_test_fixture"
    assert payload["user_concept_id"] == "#V#zhan_von_witbrock"
    assert payload["organisation"]["concept_id"] == "#V#university_of_auckland_strong_ai_lab"
    assert payload["window_session_id"] == "ws_browser_test"
    assert payload["fixture"]["messages"]["total"] == 3
