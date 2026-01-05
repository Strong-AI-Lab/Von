"""Tests for per-user preference (language & organisation) routes.

Covers /api/settings/user_prefs/<user_concept_id> GET/POST behaviour while
stubbing out DB access. These preferences are stored as relationships on the
user concept.
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

    import src.backend.services.concept_service as concept_service

    monkeypatch.setattr(concept_service, "get_concept_by_concept_id", lambda **_: None)

    resp = client.get("/api/settings/user_prefs/%23V%23missing_user")

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "User concept not found"


def test_get_user_prefs_reads_relationship_values(app_client, monkeypatch):
    _, client = app_client

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

    resp = client.get("/api/settings/user_prefs/%23V%23lu_yunli")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["preferred_language"] == "en-NZ"
    assert data["organisation_concept_id"] == "#V#the_lu_witbrock_household"


def test_set_user_prefs_updates_relationships_via_concept_service(
    app_client, monkeypatch
):
    _, client = app_client

    import src.backend.services.concept_service as concept_service

    def _fake_get_concept_by_concept_id(*, concept_id: str, **_kwargs):
        return {
            "concept_id": concept_id,
            "relationships": {
                "#V#some_other_predicate": ["#V#untouched"],
                "#V#preferred_language": ["en"],
                "#V#member_of_organisation": ["#V#old_org"],
            },
        }

    captured = {}

    def _fake_update_concept(concept_id: str, update_data):
        captured["concept_id"] = concept_id
        captured["update_data"] = update_data
        return {"concept_id": concept_id}

    monkeypatch.setattr(
        concept_service, "get_concept_by_concept_id", _fake_get_concept_by_concept_id
    )
    monkeypatch.setattr(concept_service, "update_concept", _fake_update_concept)

    resp = client.post(
        "/api/settings/user_prefs/%23V%23lu_yunli",
        json={
            "preferred_language": "en-NZ",
            "organisation_concept_id": "#V#the_lu_witbrock_household",
        },
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "success"
    assert data["preferred_language"] == "en-NZ"
    assert data["organisation_concept_id"] == "#V#the_lu_witbrock_household"

    assert captured["concept_id"] == "#V#lu_yunli"
    rel = captured["update_data"]["relationships"]
    assert rel["#V#some_other_predicate"] == ["#V#untouched"]
    assert rel["#V#preferred_language"] == ["en-NZ"]
    assert rel["#V#member_of_organisation"] == ["#V#the_lu_witbrock_household"]
