from __future__ import annotations

import time

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
            "status": "available",
            "status_label": "Available",
            "display_name": "Zhan von Witbrock",
            "email": "zhanvonwitbrock@gmail.com",
            "identity_label": "Zhan von Witbrock <zhanvonwitbrock@gmail.com>",
            "setup_hint": "Browser-test auth is available on localhost.",
            "fixture_id": "browser_user_view.v1",
        },
    )

    response = client.get("/von/api/auth/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["authenticated"] is False
    assert payload["browser_test_mode"]["available"] is True
    assert payload["browser_test_mode"]["status"] == "available"
    assert payload["browser_test_mode"]["identity_label"] == "Zhan von Witbrock <zhanvonwitbrock@gmail.com>"
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

    login_calls = []

    def _fake_login(window_session_id=None, refresh_fixture=None):
        login_calls.append(
            {
                "window_session_id": window_session_id,
                "refresh_fixture": refresh_fixture,
            }
        )
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
                "status": "not_refreshed",
                "refresh_requested": False,
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
    assert login_calls == [
        {
            "window_session_id": "ws_browser_test",
            "refresh_fixture": None,
        }
    ]


def test_exchange_token_clears_scope_when_authenticated_user_changes(app_client):
    _, client = app_client
    token = "cross-account-token"
    auth_routes._oauth_states[token] = {
        "timestamp": time.time(),
        "user": {
            "concept_id": "#V#user_b",
            "email": "user-b@example.test",
            "name": "User B",
        },
    }
    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user_a"
        sess["user_email"] = "user-a@example.test"
        sess["organisation_concept_id"] = "secret_org_a"
        sess["role_in_org"] = "owner"
        sess["namespace"] = "#V#user_a@secret_org_a"
        sess["session_id"] = "secret_chat_a"

    response = client.post(
        "/von/api/auth/exchange-token",
        json={"token": token},
    )

    assert response.status_code == 200
    with client.session_transaction() as sess:
        assert sess["user_concept_id"] == "#V#user_b"
        assert sess["user_email"] == "user-b@example.test"
        assert "organisation_concept_id" not in sess
        assert "role_in_org" not in sess
        assert "namespace" not in sess
        assert "session_id" not in sess


def test_logout_invalidates_owned_window_context(monkeypatch, app_client):
    _, client = app_client
    from src.backend.services import window_session_context_service as window_service

    store = window_service.WindowSessionStore()
    monkeypatch.setattr(window_service, "_window_session_store", store)
    ctx = store.get_or_create("owned_window", "#V#user_a")
    ctx.organisation_concept_id = "secret_org_a"
    ctx.namespace = "#V#user_a@secret_org_a"
    store.set(ctx)
    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user_a"
        sess["user_email"] = "user-a@example.test"

    response = client.post(
        "/von/api/auth/logout",
        headers={"X-Von-Window-Session": "owned_window"},
    )

    assert response.status_code == 200
    assert store.get("owned_window") is None


def test_logout_clears_auth_when_durable_window_cleanup_is_unavailable(
    monkeypatch, app_client
):
    _, client = app_client
    from src.backend.services import window_session_context_service as window_service
    from src.backend.services.window_session_binding_store_service import (
        WindowSessionBindingStoreUnavailable,
    )

    class UnavailableDeleteRepository:
        def delete_owned(self, *_args, **_kwargs):
            raise WindowSessionBindingStoreUnavailable("unavailable")

    store = window_service.WindowSessionStore(
        binding_repository=UnavailableDeleteRepository()  # type: ignore[arg-type]
    )
    monkeypatch.setattr(window_service, "_window_session_store", store)
    store.set(
        window_service.WindowSessionContext(
            window_session_id="owned_window_store_down",
            user_id="#V#user_a",
            organisation_concept_id="#V#secret_org_a",
            namespace="#V#user_a@secret_org_a",
        )
    )
    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user_a"
        sess["user_email"] = "user-a@example.test"

    response = client.post(
        "/von/api/auth/logout",
        headers={"X-Von-Window-Session": "owned_window_store_down"},
    )

    assert response.status_code == 200
    assert response.get_json()["window_context_cleanup"] == "deferred"
    with client.session_transaction() as sess:
        assert "user_concept_id" not in sess
        assert "user_email" not in sess
    assert store.get("owned_window_store_down") is None
