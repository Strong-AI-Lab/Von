"""Tests for actor-owned, non-authority user preference routes.

Covers /api/settings/user_prefs/<user_concept_id> GET/POST behaviour while
stubbing out DB access. Organisation membership has its own lifecycle and is
never writable through this surface.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def app_client(monkeypatch):
    """Provide a Flask test client with side effects stubbed out."""

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

    fake_flow_module.Flow = _DummyFlow  # type: ignore[attr-defined]
    sys.modules["google_auth_oauthlib"] = types.ModuleType("google_auth_oauthlib")
    sys.modules["google_auth_oauthlib"].flow = fake_flow_module  # type: ignore[attr-defined]
    sys.modules["google_auth_oauthlib.flow"] = fake_flow_module

    fake_id_token_module = types.ModuleType("google.oauth2.id_token")
    fake_id_token_module.verify_oauth2_token = lambda *args, **kwargs: {  # type: ignore[attr-defined]
        "sub": "dummy-user"
    }

    fake_credentials_module = types.ModuleType("google.oauth2.credentials")

    class _DummyCredentials:
        def __init__(self, id_token: str = "dummy-token"):
            self.id_token = id_token

    fake_credentials_module.Credentials = _DummyCredentials  # type: ignore[attr-defined]

    fake_service_account_module = types.ModuleType("google.oauth2.service_account")

    class _DummyServiceAccountCredentials:
        def __init__(self, *args, **kwargs):
            self.project_id = kwargs.get("project_id")

    fake_service_account_module.Credentials = _DummyServiceAccountCredentials  # type: ignore[attr-defined]

    fake_oauth2_package = types.ModuleType("google.oauth2")
    fake_oauth2_package.id_token = fake_id_token_module  # type: ignore[attr-defined]
    fake_oauth2_package.credentials = fake_credentials_module  # type: ignore[attr-defined]
    fake_oauth2_package.service_account = fake_service_account_module  # type: ignore[attr-defined]

    sys.modules["google.oauth2"] = fake_oauth2_package
    sys.modules["google.oauth2.id_token"] = fake_id_token_module
    sys.modules["google.oauth2.credentials"] = fake_credentials_module
    sys.modules["google.oauth2.service_account"] = fake_service_account_module

    import src.backend.server.utils_flask as utils_flask

    # Avoid starting the DB monitor thread or touching Mongo during tests.
    monkeypatch.setattr(utils_flask, "ensure_monitor_started", lambda: None)
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


def test_get_user_prefs_returns_404_when_user_concept_missing(app_client, monkeypatch):
    _, client = app_client

    import src.backend.server.routes.settings_routes as settings_routes
    import src.backend.services.concept_service as concept_service

    monkeypatch.setattr(
        settings_routes,
        "get_effective_user_concept_id",
        lambda: "#V#missing_user",
    )
    monkeypatch.setattr(concept_service, "get_concept_by_concept_id", lambda **_: None)

    resp = client.get("/api/settings/user_prefs/%23V%23missing_user")

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "User concept not found"


def test_get_user_prefs_reads_relationship_values(app_client, monkeypatch):
    _, client = app_client

    import src.backend.server.routes.settings_routes as settings_routes
    import src.backend.services.concept_service as concept_service

    def _fake_get_concept_by_concept_id(*, concept_id: str, **_kwargs):
        assert concept_id == "#V#lu_yunli"
        return {
            "concept_id": concept_id,
            "relationships": {
                "#V#preferred_language": ["en-NZ"],
                "#V#member_of_organisation": ["#V#the_lu_witbrock_household"],
            },
        }

    monkeypatch.setattr(
        concept_service, "get_concept_by_concept_id", _fake_get_concept_by_concept_id
    )
    monkeypatch.setattr(
        settings_routes,
        "get_effective_user_concept_id",
        lambda: "#V#lu_yunli",
    )

    resp = client.get("/api/settings/user_prefs/%23V%23lu_yunli")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["preferred_language"] == "en-NZ"
    assert data["organisation_concept_id"] == "#V#the_lu_witbrock_household"


def test_set_user_language_uses_governed_text_effect(app_client, monkeypatch):
    _, client = app_client

    import src.backend.server.routes.settings_routes as settings_routes
    import src.backend.services.concept_service as concept_service
    import src.backend.services.ontology_mutation_command_service as command

    def _fake_get_concept_by_concept_id(*, concept_id: str, **_kwargs):
        return {"concept_id": concept_id, "relationships": {}}

    captured: dict = {}

    def _fake_execute(**kwargs):
        captured["arguments"] = kwargs["arguments"]
        captured["mutation"] = kwargs["mutate"]()
        return {
            "success": True,
            "changed": True,
            "authority_receipt": {"receipt_id": "receipt-language"},
            "canonical_read_back": {"text_sha256": "exact"},
        }

    monkeypatch.setattr(
        settings_routes,
        "get_effective_user_concept_id",
        lambda: "#V#lu_yunli",
    )
    monkeypatch.setattr(
        concept_service, "get_concept_by_concept_id", _fake_get_concept_by_concept_id
    )
    monkeypatch.setattr(command, "execute_governed_ontology_method", _fake_execute)
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        lambda **kwargs: captured.setdefault("text_write", kwargs) or {"success": True},
    )

    resp = client.post(
        "/api/settings/user_prefs/%23V%23lu_yunli",
        json={"preferred_language": "en-NZ", "request_id": "pref-language-1"},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "success"
    assert data["preferred_language"] == "en-NZ"
    assert data["organisation_concept_id"] is None
    assert data["authority_receipt"] == {"receipt_id": "receipt-language"}

    assert captured["arguments"] == {
        "concept_id": "#V#lu_yunli",
        "predicate": "#V#preferred_language",
        "text": "en-NZ",
        "language": "en-NZ",
        "context": {"preference": "preferred_language"},
        "provenance": {
            "source": "user_preferences",
            "actor_concept_id": "#V#lu_yunli",
        },
        "request_id": "pref-language-1",
    }
    assert captured["text_write"]["subject_concept_id"] == "#V#lu_yunli"


def test_set_user_prefs_cannot_change_organisation_membership(
    app_client,
    monkeypatch,
):
    _, client = app_client

    import src.backend.server.routes.settings_routes as settings_routes

    monkeypatch.setattr(
        settings_routes,
        "get_effective_user_concept_id",
        lambda: "#V#lu_yunli",
    )
    response = client.post(
        "/api/settings/user_prefs/%23V%23lu_yunli",
        json={"organisation_concept_id": "#V#forged_org"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == (
        "organisation_membership_requires_dedicated_lifecycle"
    )
