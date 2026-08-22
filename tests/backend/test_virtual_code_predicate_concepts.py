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
    assert "#V#organisation_ontology_administrator" not in ids
    assert "#V#global_ontology_administrator" not in ids
    assert "#V#von_operational_administrator" not in ids

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


def test_search_marks_virtual_renderer_profile_predicate_as_text(
    app_client, monkeypatch
):
    _, client = app_client

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.ConceptsRepository.find",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "src.backend.services.concept_search_service.TextRelationsRepository.find",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "src.backend.services.concept_search_service.TextValuesRepository.find",
        lambda *a, **k: [],
    )

    resp = client.get(
        "/vontology/api/vontology/search"
        "?q=renderer&filter_kind=predicate&include_predicate_metadata=true"
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["results"] == [
        {
            "id": "#V#has_renderer_profile_json",
            "name": "has_renderer_profile_json",
            "kind": "predicate",
            "relevance_score": 74.8,
            "is_text_predicate": True,
        }
    ]


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
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("legacy scan used")),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.relationship_extent_index_ready",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.incoming_dynamic_extent_rows_for_target",
        lambda *a, **k: (
            [
                {
                    "relation_id": "struct::#V#other::#V#attended_event::incoming::0",
                    "source": "structured",
                    "relation_kind": "binary",
                    "role": "arg2",
                    "predicate_id": "#V#attended_event",
                    "arg1_value": "#V#other",
                    "arg1_is_concept": True,
                    "arg2_value": "#V#focus",
                    "arg2_is_concept": True,
                    "arg2_index": 2,
                    "source_concept_id": "#V#other",
                    "target_value": "#V#focus",
                    "is_asserted": True,
                    "relation_state": "asserted",
                }
            ],
            True,
        ),
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


def test_relationship_extent_route_dedupes_visibility_alias_rows(
    app_client, monkeypatch
):
    _, client = app_client

    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.find_one",
        lambda *a, **k: {"concept_id": "#V#focus", "relationships": {}},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.build_concept_relations_payload",
        lambda *a, **k: {
            "relations": [
                {
                    "relation_id": "struct::#V#focus::specific_to_user",
                    "source_concept_id": "#V#focus",
                    "predicate_id": "specific_to_user",
                    "relation_kind": "binary",
                    "target_values": ["#V#michael_witbrock"],
                    "matched_argument_indexes": [1],
                },
                {
                    "relation_id": "struct::#V#focus::#V#specific_to_user",
                    "source_concept_id": "#V#focus",
                    "predicate_id": "#V#specific_to_user",
                    "relation_kind": "binary",
                    "target_values": ["#V#michael_witbrock"],
                    "matched_argument_indexes": [1],
                },
            ]
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.relationship_extent_index_ready",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.incoming_dynamic_extent_rows_for_target",
        lambda *a, **k: ([], False),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.aggregate",
        lambda *a, **k: [],
    )

    resp = client.get(
        "/vontology/api/vontology/relationships/extent?concept_id=%23V%23focus"
    )
    assert resp.status_code == 200
    rows = [
        row
        for row in resp.get_json().get("rows") or []
        if row.get("predicate_id") == "#V#specific_to_user"
    ]
    assert len(rows) == 1
    assert rows[0]["arg1_value"] == "#V#focus"
    assert rows[0]["arg2_value"] == "#V#michael_witbrock"


def test_relationship_extent_route_dedupes_indexed_visibility_alias_rows(
    app_client, monkeypatch
):
    _, client = app_client

    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.find_one",
        lambda *a, **k: {"concept_id": "#V#michael_witbrock", "relationships": {}},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.build_concept_relations_payload",
        lambda *a, **k: {"relations": []},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.relationship_extent_index_ready",
        lambda: True,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.incoming_dynamic_extent_rows_page_for_target",
        lambda *a, **k: (
            [
                {
                    "relation_id": "struct::#V#focus::specific_to_user::incoming::0",
                    "source": "structured",
                    "relation_kind": "binary",
                    "role": "arg2",
                    "predicate_id": "specific_to_user",
                    "arg1_value": "#V#focus",
                    "arg1_is_concept": True,
                    "arg2_value": "#V#michael_witbrock",
                    "arg2_is_concept": True,
                    "arg2_index": 2,
                    "source_concept_id": "#V#focus",
                    "target_value": "#V#michael_witbrock",
                    "is_asserted": True,
                    "relation_state": "asserted",
                },
                {
                    "relation_id": "struct::#V#focus::#V#specific_to_user::incoming::0",
                    "source": "structured",
                    "relation_kind": "binary",
                    "role": "arg2",
                    "predicate_id": "#V#specific_to_user",
                    "arg1_value": "#V#focus",
                    "arg1_is_concept": True,
                    "arg2_value": "#V#michael_witbrock",
                    "arg2_is_concept": True,
                    "arg2_index": 2,
                    "source_concept_id": "#V#focus",
                    "target_value": "#V#michael_witbrock",
                    "is_asserted": True,
                    "relation_state": "asserted",
                },
            ],
            True,
            {"used_extent_index": True, "complete": True, "bounded": False},
        ),
    )

    resp = client.get(
        "/vontology/api/vontology/relationships/extent?"
        "concept_id=%23V%23michael_witbrock&role=arg2&source=structured"
    )
    assert resp.status_code == 200
    rows = resp.get_json().get("rows") or []
    assert len(rows) == 1
    assert rows[0]["predicate_id"] == "#V#specific_to_user"
    assert rows[0]["arg1_value"] == "#V#focus"
    assert rows[0]["arg2_value"] == "#V#michael_witbrock"


def test_relationship_extent_route_uses_bounded_incoming_index_page(
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
        "src.backend.server.routes.vontology_routes.relationship_extent_index_ready",
        lambda: True,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.incoming_dynamic_extent_rows_for_target",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("unbounded incoming extent helper used")
        ),
    )

    def _fake_page(*_args, **kwargs):
        assert kwargs["visible_offset"] == 0
        assert kwargs["visible_limit"] == 3
        return (
            [
                {
                    "relation_id": "struct::#V#other::#V#attended_event::incoming::0",
                    "source": "structured",
                    "relation_kind": "binary",
                    "role": "arg2",
                    "predicate_id": "#V#attended_event",
                    "arg1_value": "#V#other",
                    "arg1_is_concept": True,
                    "arg2_value": "#V#focus",
                    "arg2_is_concept": True,
                    "arg2_index": 2,
                    "source_concept_id": "#V#other",
                    "target_value": "#V#focus",
                    "is_asserted": True,
                    "relation_state": "asserted",
                }
            ],
            True,
            {
                "used_extent_index": True,
                "complete": False,
                "bounded": True,
                "has_more": True,
                "index_rows_scanned": 128,
                "source_concepts_access_checked": 100,
                "rows_filtered_by_access": 99,
                "rows_returned": 1,
            },
        )

    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.incoming_dynamic_extent_rows_page_for_target",
        _fake_page,
    )

    resp = client.get(
        "/vontology/api/vontology/relationships/extent?"
        "concept_id=%23V%23focus&role=arg2&source=structured&limit=2"
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["total_is_complete"] is False
    assert payload["has_more"] is True
    assert payload["extent_index"]["index_rows_scanned"] == 128
    assert payload["rows"][0]["arg1_value"] == "#V#other"


def test_relationship_extent_route_falls_back_before_extent_index_build(
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
        "src.backend.server.routes.vontology_routes.relationship_extent_index_ready",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.incoming_dynamic_extent_rows_for_target",
        lambda *a, **k: ([], False),
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
    rows = resp.get_json().get("rows") or []
    assert rows
    assert rows[0]["role"] == "arg2"
    assert rows[0]["predicate_id"] == "#V#attended_event"


def test_relationship_extent_route_supports_uncertain_filters(app_client, monkeypatch):
    _, client = app_client

    captured_kwargs = {}

    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.find_one",
        lambda *a, **k: {"concept_id": "#V#focus", "relationships": {}},
    )

    def _fake_build_payload(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return {
            "relations": [
                {
                    "relation_id": "uncertain::#V#focus::u1",
                    "source_concept_id": "#V#focus",
                    "predicate_id": "#V#related_to",
                    "relation_kind": "binary",
                    "target_values": ["#V#candidate"],
                    "matched_argument_indexes": [1],
                    "is_asserted": False,
                    "relation_state": "uncertain",
                    "uncertainty": {
                        "assertion_id": "u1",
                        "status": "proposed",
                        "confidence_score": 0.77,
                        "provenance": {"source": "unit_test"},
                    },
                }
            ]
        }

    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.build_concept_relations_payload",
        _fake_build_payload,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.vontology_routes.ConceptsRepository.aggregate",
        lambda *a, **k: [],
    )

    resp = client.get(
        "/vontology/api/vontology/relationships/extent?"
        "concept_id=%23V%23focus&uncertainty_mode=uncertain_only&source=uncertain_assertions"
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    rows = payload.get("rows") or []
    assert len(rows) == 1
    assert rows[0]["source"] == "uncertain_assertions"
    assert rows[0]["relation_state"] == "uncertain"
    assert rows[0]["is_asserted"] is False
    assert rows[0]["uncertainty"]["assertion_id"] == "u1"
    assert captured_kwargs.get("uncertainty_mode") == "uncertain_only"


def test_uncertain_relationship_promote_route_returns_success_payload(
    app_client, monkeypatch
):
    _, client = app_client

    monkeypatch.setattr(
        "src.backend.services.uncertain_relationship_service.promote_uncertain_relationship_assertion",
        lambda **kwargs: {
            "success": True,
            "assertion": {"assertion_id": kwargs.get("assertion_id"), "status": "promoted"},
            "write_result": {"success": True},
        },
    )

    resp = client.post(
        "/vontology/api/vontology/relationships/uncertain/promote",
        json={"source_id": "#V#focus", "assertion_id": "u1", "operator": "unit_test"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload.get("success") is True
    assert payload.get("assertion", {}).get("status") == "promoted"


def test_uncertain_relationship_reject_route_requires_reason(app_client):
    _, client = app_client

    resp = client.post(
        "/vontology/api/vontology/relationships/uncertain/reject",
        json={"source_id": "#V#focus", "assertion_id": "u1"},
    )
    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload.get("success") is False
    assert "reason" in (payload.get("error") or "").lower()


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
