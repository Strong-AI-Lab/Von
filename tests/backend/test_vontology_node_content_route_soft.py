"""Tests for soft-missing behaviour on the Vontology node content route.

The chat UI may probe many historical concept IDs which no longer exist.
For that use-case, the route supports `soft=1` to return 200 (not 404) with a
structured `not_found` marker.
"""

from __future__ import annotations

import types

import pytest


@pytest.mark.parametrize("name_predicate", ["#V#hasName", "hasName", None])
def test_raw_task_metadata_uses_name_aliases_and_preserves_link_identity(
    app_client, monkeypatch, name_predicate
):
    from src.backend.server.routes import vontology_routes as routes
    from src.backend.services import vontology_concept_stats_service as stats

    _, client = app_client
    task_id = "#V#task_agent_opaque_identifier"
    title = "Implement slideable desktop conversation tray"
    stored_names = [{"name": title, "language": "en-NZ", "type": "NL"}]
    monkeypatch.setattr(
        routes,
        "get_vontology_node_content",
        lambda *a, **k: {
            "concept_id": task_id,
            "display_name": "Task Agent Opaque Identifier",
            "kind": "individual",
            "raw_doc": {
                "concept_id": task_id,
                "names": stored_names if name_predicate is None else [],
                "relationships": {"is_an_instance_of": ["#V#task_specification"]},
            },
        },
    )
    monkeypatch.setattr(stats, "get_vontology_concept_stats", lambda *a, **k: {})
    calls = []

    def read_names(ids, **kwargs):
        calls.append((ids, kwargs))
        assert set(kwargs["predicates"]) == {"hasName", "#V#hasName"}
        return {
            task_id: [
                {
                    "text": title,
                    "lang": "en-NZ",
                    "predicate": name_predicate,
                    "context": {},
                }
            ]
            if name_predicate
            else []
        }

    monkeypatch.setattr(routes, "get_texts_for_concepts", read_names)
    response = client.get(
        "/vontology/api/vontology/node_content",
        query_string={"identifier": task_id, "raw_only": "1"},
    )
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["concept_id"] == payload["raw_doc"]["concept_id"] == task_id
    assert payload["raw_doc"]["names"][0]["name"] == title
    if name_predicate:
        assert payload["display_name"] == title
    assert len(calls) == 1


@pytest.fixture
def app_client(monkeypatch):
    # Stub Google auth deps pulled in by utils_flask -> auth_routes imports.
    import sys

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


def test_node_content_default_returns_404_for_missing_concept(app_client, monkeypatch):
    _, client = app_client

    def _fake_get(_identifier: str, **_kwargs):
        return {"error": "Concept '#V#missing' not found in MongoDB."}

    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.get_vontology_node_content",
        _fake_get,
    )

    resp = client.get("/vontology/api/vontology/node_content?identifier=%23V%23missing")

    assert resp.status_code == 404
    payload = resp.get_json()
    assert payload["error"] == "Concept '#V#missing' not found in MongoDB."
    assert payload.get("not_found") is None


def test_node_content_soft_returns_200_with_not_found_marker(app_client, monkeypatch):
    _, client = app_client

    calls = []

    def _fake_get(_identifier: str, **kwargs):
        calls.append((_identifier, kwargs))
        return {"error": "Concept '#V#missing' not found in MongoDB."}

    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.get_vontology_node_content",
        _fake_get,
    )

    resp = client.get(
        "/vontology/api/vontology/node_content?identifier=%23V%23missing&soft=1&raw_only=1"
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["error"] == "Concept '#V#missing' not found in MongoDB."
    assert payload["not_found"] is True
    assert calls == [
        ("#V#missing", {"resolve_display_name": False}),
    ]


def test_node_content_returns_403_for_access_denied_exact_concept(
    app_client, monkeypatch
):
    _, client = app_client

    def _fake_get(_identifier: str, **_kwargs):
        return {
            "error": "Concept '#V#private' is not accessible in the current context.",
            "error_code": "access_denied",
            "concept_id": "#V#private",
            "access": {"exists": True, "accessible": False},
        }

    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.get_vontology_node_content",
        _fake_get,
    )

    resp = client.get("/vontology/api/vontology/node_content?identifier=%23V%23private")

    assert resp.status_code == 403
    payload = resp.get_json()
    assert payload["error_code"] == "access_denied"
    assert payload["access"]["exists"] is True


def test_concept_route_returns_403_when_exact_concept_exists_but_is_inaccessible(
    app_client, monkeypatch
):
    _, client = app_client

    from src.backend.services.concept_service import ConceptNotFoundError

    def _raise_not_found(_concept_id: str):
        raise ConceptNotFoundError("concept with concept_id '#V#private' not found.")

    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.concept_service.get_concept_by_concept_id",
        _raise_not_found,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.describe_concept_access",
        lambda _concept_id: {"exists": True, "accessible": False},
    )

    resp = client.get("/api/concepts/%23V%23private")

    assert resp.status_code == 403
    payload = resp.get_json()
    assert payload["error_code"] == "access_denied"
    assert payload["access"]["accessible"] is False
