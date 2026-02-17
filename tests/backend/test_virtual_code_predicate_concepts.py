"""Regression tests for virtual code-handled predicate concepts.

These predicates (e.g. #V#hasContent) may be stored as text-relation predicates even
when no MongoDB concept document exists. The UI should still be able to render
cartouches/tabs/tree/instances without prompting to create a missing concept.
"""

from __future__ import annotations

import sys
import types

import pytest

from src.backend.vontology import utils_vontology


def _flatten_tree_ids(node: dict) -> list[str]:
    out: list[str] = []

    def rec(n: dict):
        if not isinstance(n, dict):
            return
        cid = n.get("id") or n.get("concept_id")
        if isinstance(cid, str):
            out.append(cid)
        for ch in n.get("children") or []:
            rec(ch)

    rec(node)
    return out


def test_node_content_virtual_code_predicate_when_missing(monkeypatch):
    monkeypatch.setattr(
        utils_vontology.ConceptsRepository, "find_one", lambda *a, **k: None
    )

    payload = utils_vontology.get_vontology_node_content("#V#hasContent")

    assert "error" not in payload
    assert payload.get("concept_id") == "#V#hasContent"
    assert payload.get("kind") == "predicate"
    assert payload.get("display_name") == "hasContent"


def test_tree_includes_virtual_code_predicates_under_predicate_type(monkeypatch):
    def _fake_find(*_args, **_kwargs):
        return [
            {
                "concept_id": "#V#thing",
                "name": "Thing",
                "names": [{"name": "Thing", "type": "NL", "language": "en-NZ"}],
                "relationships": {"is_a_type_of": [], "is_an_instance_of": []},
                "path": "#V#thing",
            },
            {
                "concept_id": "#V#predicate",
                "name": "Predicate",
                "names": [{"name": "Predicate", "type": "NL", "language": "en-NZ"}],
                "relationships": {
                    "is_a_type_of": ["#V#thing"],
                    "is_an_instance_of": [],
                },
                "path": "#V#predicate",
            },
        ]

    monkeypatch.setattr(utils_vontology.ConceptsRepository, "find", _fake_find)

    tree_payload = utils_vontology.get_vontology_tree()
    assert "error" not in tree_payload
    tree = tree_payload.get("tree")
    assert isinstance(tree, list) and tree

    root = tree[0]
    ids = _flatten_tree_ids(root)

    assert "#V#hasContent" in ids

    # Also check the structural expectation: hasContent is placed somewhere under #V#predicate.
    # We do this by locating the predicate node and verifying it contains the child.
    def find_node(n: dict, target: str) -> dict | None:
        if not isinstance(n, dict):
            return None
        if n.get("id") == target:
            return n
        for ch in n.get("children") or []:
            found = find_node(ch, target)
            if found:
                return found
        return None

    predicate_node = find_node(root, "#V#predicate")
    assert predicate_node is not None
    predicate_child_ids = _flatten_tree_ids(predicate_node)
    assert "#V#hasContent" in predicate_child_ids


@pytest.fixture
def app_client(monkeypatch):
    """Provide a Flask test client with side effects stubbed out."""

    # Stub Google auth dependencies pulled in by utils_flask -> auth_routes imports.
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


def test_instances_includes_virtual_code_concepts_for_mentioned_in_von_code(
    app_client, monkeypatch
):
    _, client = app_client

    # Avoid hitting Mongo; ensure route still exposes code concepts.
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.find",
        lambda *a, **k: [],
    )

    resp = client.get(
        "/vontology/api/vontology/instances?node_id=%23V%23mentioned_in_von_code"
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    ids = {item.get("id") for item in payload.get("instances") or []}
    assert "#V#hasContent" in ids
    assert "#V#has_blob_uri" in ids


def test_relationships_route_allows_virtual_code_predicate(app_client, monkeypatch):
    _, client = app_client

    # Ensure the primary concept lookup misses Mongo.
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.find_one",
        lambda *a, **k: None,
    )
    # Avoid any accidental Mongo reads during ID resolution.
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.find",
        lambda *a, **k: [],
    )

    resp = client.get(
        "/vontology/api/vontology/relationships?identifier=%23V%23hasContent"
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload.get("success") is True
    assert payload.get("concept_id") == "#V#hasContent"


def test_relationships_route_normalises_has_subtypes_alias(app_client, monkeypatch):
    _, client = app_client

    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.find_one",
        lambda *a, **k: {
            "concept_id": "#V#parent",
            "relationships": {"has_subtypes": ["#V#child"]},
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.find",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.get_most_salient_type",
        lambda *a, **k: None,
    )

    resp = client.get(
        "/vontology/api/vontology/relationships?identifier=%23V%23parent"
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    rel = payload.get("relationships") or {}

    assert "has_subtypes" not in rel
    assert rel.get("has_subtype") == [
        {"id": "#V#child", "name": "#V#child", "kind": "individual"}
    ]


def test_relationships_route_normalises_is_a_type_ofs_alias(app_client, monkeypatch):
    _, client = app_client

    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.find_one",
        lambda *a, **k: {
            "concept_id": "#V#child",
            "relationships": {"is_a_type_ofs": ["#V#parent"]},
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.find",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.get_most_salient_type",
        lambda *a, **k: None,
    )

    resp = client.get(
        "/vontology/api/vontology/relationships?identifier=%23V%23child"
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    rel = payload.get("relationships") or {}

    assert "is_a_type_ofs" not in rel
    assert rel.get("is_a_type_of") == [
        {"id": "#V#parent", "name": "#V#parent", "kind": "individual"}
    ]


def test_relationship_extent_route_returns_outgoing_rows(app_client, monkeypatch):
    _, client = app_client

    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.find_one",
        lambda *a, **k: {"concept_id": "#V#focus", "relationships": {"related_to": []}},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.build_concept_relations_payload",
        lambda *a, **k: {
            "relations": [
                {
                    "relation_id": "struct::#V#focus::related_to",
                    "source_concept_id": "#V#focus",
                    "predicate_id": "related_to",
                    "relation_kind": "binary",
                    "target_values": ["#V#target"],
                    "matched_argument_indexes": [1],
                },
                {
                    "relation_id": "text::#V#focus::hasName",
                    "source_concept_id": "#V#focus",
                    "predicate_id": "hasName",
                    "relation_kind": "text",
                    "target_values": ["Example name"],
                    "text_value": {"text": "Example name", "lang": "en-NZ"},
                    "matched_argument_indexes": [1],
                },
            ]
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.aggregate",
        lambda *a, **k: [],
    )

    resp = client.get(
        "/vontology/api/vontology/relationships/extent?concept_id=%23V%23focus"
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload.get("success") is True
    rows = payload.get("rows") or []
    assert any(
        row.get("role") == "arg1"
        and row.get("predicate_id") == "related_to"
        and row.get("arg1_value") == "#V#focus"
        and row.get("arg2_value") == "#V#target"
        for row in rows
    )
    assert any(
        row.get("relation_kind") == "text"
        and row.get("predicate_id") == "hasName"
        and row.get("arg2_value") == "Example name"
        for row in rows
    )


def test_relationship_extent_route_includes_incoming_dynamic_arg2_rows(
    app_client, monkeypatch
):
    _, client = app_client

    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.find_one",
        lambda *a, **k: {"concept_id": "#V#focus", "relationships": {}},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.build_concept_relations_payload",
        lambda *a, **k: {"relations": []},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.aggregate",
        lambda *a, **k: [
            {
                "concept_id": "#V#other",
                "predicate": "#V#attended_event",
                "targets": ["#V#focus"],
            }
        ],
    )

    resp = client.get(
        "/vontology/api/vontology/relationships/extent?concept_id=%23V%23focus&role=arg2"
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload.get("success") is True
    rows = payload.get("rows") or []
    assert rows
    assert rows[0]["role"] == "arg2"
    assert rows[0]["predicate_id"] == "#V#attended_event"
    assert rows[0]["arg1_value"] == "#V#other"
    assert rows[0]["arg2_value"] == "#V#focus"


def test_upsert_name_for_virtual_code_predicate_concept(app_client, monkeypatch):
    _, client = app_client

    # Avoid real DB writes. We only want to verify that access control no longer
    # blocks virtual code predicate concepts.
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.upsert_text_for_concept",
        lambda **kwargs: {
            "relation_id": "dummy",
            "relation_created": True,
            "context_updated": False,
            "text_id": "dummy_text_id",
        },
    )

    resp = client.post(
        "/api/concepts/%23V%23hasContent/texts",
        json={"predicate": "hasName", "text": "hasContent", "lang": "en"},
    )
    assert resp.status_code in (200, 201)
