from __future__ import annotations

from flask import Flask
import pytest

from src.backend.integrations.internal_mcp import catalogue
from src.backend.server.routes.vontology_routes import vontology_bp


def _client():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(vontology_bp, url_prefix="/api/vontology")
    return app.test_client()


def test_legacy_scope_route_fails_closed_even_with_forged_force_and_org():
    response = _client().post(
        "/api/vontology/concept/organization-relation",
        json={
            "concept_id": "#V#historical_concept",
            "organisation_concept_id": "#V#forged_organisation",
            "action": "remove",
            "force": True,
        },
    )

    assert response.status_code == 410
    payload = response.get_json()
    assert payload["error_code"] == "governed_scope_change_required"
    assert payload["effect_status"] == "not_started"


def test_legacy_user_scope_route_fails_closed_even_with_forged_user_and_force():
    response = _client().post(
        "/api/vontology/concept/user-relation",
        json={
            "concept_id": "#V#historical_concept",
            "user_concept_id": "#V#someone_else",
            "action": "remove",
            "force": "true",
        },
    )

    assert response.status_code == 410
    assert response.get_json()["error_code"] == "governed_scope_change_required"


def test_relationship_route_does_not_forward_client_identity_or_override(monkeypatch):
    seen: dict[str, object] = {}

    def fake_add_relationship(**kwargs):
        seen.update(kwargs)
        return {
            "success": False,
            "error_code": "organisation_ontology_admin_authority_required",
            "authority_decision": {"allowed": False},
        }

    monkeypatch.setattr(catalogue, "_add_relationship", fake_add_relationship)

    response = _client().post(
        "/api/vontology/relationships/add",
        json={
            "source_id": "#V#source",
            "kind": "#V#predicate",
            "target_id": "#V#target",
            "user_concept_id": "#V#forged_user",
            "organisation_concept_id": "#V#forged_organisation",
            "operator_override": True,
        },
    )

    assert response.status_code == 403
    assert seen == {
        "source_id": "#V#source",
        "predicate": "#V#predicate",
        "target": "#V#target",
        "request_id": None,
        "namespace": None,
    }


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/vontology/ensure_thing", {}),
        (
            "/api/vontology/relationships/uncertain/reject",
            {
                "source_id": "#V#source",
                "assertion_id": "forged-assertion",
                "reason": "forged operator request",
            },
        ),
        (
            "/api/vontology/relationships/remove/undo",
            {"undo_token": "forged-undo-token"},
        ),
    ],
)
def test_unsafe_legacy_mutation_routes_fail_closed(path, payload):
    response = _client().post(path, json=payload)

    assert response.status_code == 410
    result = response.get_json()
    assert result["effect_status"] == "not_started"
    assert result["mutation_outcome"] == "not_started"
