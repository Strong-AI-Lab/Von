"""Tests for Phase 2 session and organisation routes (JVNAUTOSCI-789).

These tests validate authentication requirements, namespace derivation, and
role resolution for the organisation switching endpoints exposed via the
`/von/api/session` routes.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def app_client(monkeypatch):
    """Provide a Flask test client with side effects stubbed out.

    We stub the DB monitor and prompt concept health check to avoid external
    dependencies during these route tests.
    """

    # Stub Google auth dependencies pulled in by utils_flask -> auth_routes imports.
    import sys
    import types

    fake_flow_module = types.ModuleType("google_auth_oauthlib.flow")

    class _DummyFlow:
        def __init__(self, *args, **kwargs):
            self.credentials = types.SimpleNamespace(id_token="dummy-token")
            self.redirect_uri = kwargs.get("redirect_uri")
            self.client_config = {"web": {"redirect_uris": [self.redirect_uri]}}

        @classmethod
        def from_client_config(cls, *args, **kwargs):
            return cls(**kwargs)

        def authorization_url(self, *args, **kwargs):
            return "https://auth.example", "state-token"

        def fetch_token(self, *args, **kwargs):
            return None

    fake_flow_module.Flow = _DummyFlow
    sys.modules["google_auth_oauthlib"] = types.ModuleType("google_auth_oauthlib")
    sys.modules["google_auth_oauthlib"].flow = fake_flow_module
    sys.modules["google_auth_oauthlib.flow"] = fake_flow_module

    fake_id_token_module = types.ModuleType("google.oauth2.id_token")
    fake_id_token_module.verify_oauth2_token = lambda *args, **kwargs: {
        "sub": "dummy-user"
    }

    fake_credentials_module = types.ModuleType("google.oauth2.credentials")

    class _DummyCredentials:
        def __init__(self, id_token: str = "dummy-token"):
            self.id_token = id_token

    fake_credentials_module.Credentials = _DummyCredentials

    fake_service_account_module = types.ModuleType("google.oauth2.service_account")

    class _DummyServiceAccountCredentials:
        def __init__(self, *args, **kwargs):
            self.project_id = kwargs.get("project_id")

    fake_service_account_module.Credentials = _DummyServiceAccountCredentials

    fake_oauth2_package = types.ModuleType("google.oauth2")
    fake_oauth2_package.id_token = fake_id_token_module
    fake_oauth2_package.credentials = fake_credentials_module
    fake_oauth2_package.service_account = fake_service_account_module

    sys.modules["google.oauth2"] = fake_oauth2_package
    sys.modules["google.oauth2.id_token"] = fake_id_token_module
    sys.modules["google.oauth2.credentials"] = fake_credentials_module
    sys.modules["google.oauth2.service_account"] = fake_service_account_module

    import src.backend.server.utils_flask as utils_flask

    # Avoid starting the DB monitor thread or touching Mongo during tests.
    monkeypatch.setattr(utils_flask, "ensure_monitor_started", lambda: None)
    # Keep prompt concept health check lightweight.
    monkeypatch.setattr(
        utils_flask,
        "prompt_concept_health_status",
        lambda: {"available": True, "source_field": "stub"},
    )

    app = utils_flask.create_flask_app(
        list_models_func=lambda: ["dummy-model"],
        generate_func=lambda prompt, context, model: "ok",
    )
    app.config["TESTING"] = True

    with app.test_client() as client:
        yield app, client


def test_set_organisation_requires_authentication(app_client):
    _, client = app_client

    resp = client.post(
        "/von/api/session/set_organisation", json={"organisation_concept_id": "any_org"}
    )

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Not authenticated"


def test_set_organisation_updates_session_and_namespace(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"

    resp = client.post(
        "/von/api/session/set_organisation",
        json={"organisation_concept_id": "university_of_auckland_strong_ai_lab"},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "updated"
    assert data["role"] == "admin"  # From stub role resolver mapping
    assert (
        data["namespace"] == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    )

    with client.session_transaction() as sess:
        assert sess["organisation_concept_id"] == "university_of_auckland_strong_ai_lab"
        assert sess["role_in_org"] == "admin"
        assert (
            sess["namespace"]
            == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
        )


def test_set_organisation_validates_body(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"

    resp = client.post("/von/api/session/set_organisation", json={})

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "organisation_concept_id required"


def test_get_session_context_derives_namespace_without_org(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"

    resp = client.get("/von/api/session/context")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["authenticated"] is True
    assert data["organisation_id"] is None
    assert data["role"] is None
    assert data["namespace"] == "#V#michael_witbrock"


def test_get_session_context_derives_role_and_namespace_with_org(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"
        sess["organisation_concept_id"] = "university_of_auckland_strong_ai_lab"

    resp = client.get("/von/api/session/context")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["authenticated"] is True
    assert data["role"] == "admin"  # Resolved via stub mapping
    assert (
        data["namespace"] == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    )


def test_get_my_organisations_requires_authentication(app_client):
    _, client = app_client

    resp = client.get("/von/api/organisations/my_organisations")

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Not authenticated"


def test_get_my_organisations_returns_stubbed_memberships(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"

    resp = client.get("/von/api/organisations/my_organisations")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_count"] == 1
    org = data["organisations"][0]
    assert org["concept_id"] == "university_of_auckland_strong_ai_lab"
    assert org["role"] == "admin"
    assert org["name"] == "University Of Auckland Strong Ai Lab"
