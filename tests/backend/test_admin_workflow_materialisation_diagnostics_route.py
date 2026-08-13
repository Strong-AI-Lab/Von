from __future__ import annotations

from flask import Flask

import src.backend.server.routes.admin_routes as admin_routes
from src.backend.server.routes.admin_routes import admin_bp


def _build_test_client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test-secret"
    app.config["DURABLE_WORKFLOW_STARTUP_STATUS"] = {
        "state": "ready",
        "ready": True,
    }
    app.config["DURABLE_WORKFLOW_COMPONENTS"] = {
        "testing_workflow_bootstrap": {"success": True}
    }
    app.register_blueprint(admin_bp)
    return app.test_client()


def test_workflow_materialisation_diagnostics_route_uses_app_context(
    monkeypatch,
) -> None:
    client = _build_test_client()
    captured: dict[str, object] = {}

    def _fake_build_workflow_materialisation_diagnostics(**kwargs):
        captured.update(kwargs)
        return {"success": True, "classification": {"state": "healthy"}}

    monkeypatch.setattr(
        admin_routes,
        "build_workflow_materialisation_diagnostics",
        _fake_build_workflow_materialisation_diagnostics,
    )
    monkeypatch.setenv("VON_ADMIN_TOKEN", "operator-token")

    response = client.get(
        "/admin/workflow_materialisation_diagnostics"
        "?required_concept_id=%23V%23ephemeral_theory"
        "&required_concept_ids=%23V%23experiment_spec,%23V%23experiment_run"
        "&include_present_concepts=false",
        headers={"X-Von-Admin-Token": "operator-token"},
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload == {"success": True, "classification": {"state": "healthy"}}
    assert captured["required_concept_ids"] == [
        "#V#ephemeral_theory",
        "#V#experiment_spec",
        "#V#experiment_run",
    ]
    assert captured["include_present_concepts"] is False
    assert captured["startup_status"] == {"state": "ready", "ready": True}
    assert captured["workflow_components"] == {
        "testing_workflow_bootstrap": {"success": True}
    }


def test_workflow_materialisation_diagnostics_route_requires_admin_authority(
    monkeypatch,
) -> None:
    client = _build_test_client()
    calls: list[object] = []
    monkeypatch.setattr(
        admin_routes,
        "build_workflow_materialisation_diagnostics",
        lambda **_kwargs: calls.append(object()),
    )

    response = client.get("/admin/workflow_materialisation_diagnostics")

    assert response.status_code == 403
    assert response.get_json() == {
        "error": "trusted_operator_authority_required"
    }
    assert calls == []


def test_org_admin_cannot_probe_global_workflow_concept_ids(
    monkeypatch,
) -> None:
    client = _build_test_client()
    calls: list[object] = []
    monkeypatch.setenv("VON_ADMIN_TOKEN", "operator-token")
    monkeypatch.setattr(
        admin_routes,
        "build_workflow_materialisation_diagnostics",
        lambda **_kwargs: calls.append(object()),
    )
    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#other_org_admin"
        flask_session["organisation_concept_id"] = "other_org"
        flask_session["role_in_org"] = "admin"

    response = client.get(
        "/admin/workflow_materialisation_diagnostics"
        "?required_concept_id=%23V%23hidden_workflow"
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "error": "trusted_operator_authority_required"
    }
    assert calls == []


def test_global_semantic_ontology_admin_cannot_use_operator_only_diagnostics(
    monkeypatch,
) -> None:
    from src.backend.services import ontology_publication_authority_service as authority

    client = _build_test_client()
    calls: list[object] = []
    semantic_admin = "#V#semantic_global_admin"
    global_role = authority.AuthorityRoleEvidence(
        role=authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
        actor_concept_id=semantic_admin,
        organisation_concept_id=None,
        relation_id="role:semantic-global-admin",
        revision="operator-separation-test",
    )
    monkeypatch.setattr(
        authority,
        "resolve_live_semantic_roles",
        lambda actor: (global_role,) if actor == semantic_admin else (),
    )
    monkeypatch.setattr(
        admin_routes,
        "build_workflow_materialisation_diagnostics",
        lambda **_kwargs: calls.append(object()),
    )
    # Configure the distinct operator credential, but do not give it to the
    # represented global semantic administrator.
    monkeypatch.setenv("VON_ADMIN_TOKEN", "operator-token")
    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = semantic_admin

    authority_summary = authority.list_actor_semantic_authority(semantic_admin)
    assert [item["role"] for item in authority_summary["roles"]] == [
        authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE
    ]

    response = client.get("/admin/workflow_materialisation_diagnostics")

    assert response.status_code == 403
    assert response.get_json() == {
        "error": "trusted_operator_authority_required"
    }
    assert calls == []


def test_represented_operational_administrator_can_use_diagnostics(
    monkeypatch,
) -> None:
    from src.backend.services import von_operational_administrator_service as operators

    client = _build_test_client()
    calls: list[object] = []
    monkeypatch.setattr(
        operators,
        "is_live_von_operational_administrator",
        lambda actor: actor == "#V#operational_admin",
    )
    monkeypatch.setattr(
        admin_routes,
        "build_workflow_materialisation_diagnostics",
        lambda **_kwargs: calls.append(object()) or {"success": True},
    )
    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#operational_admin"

    response = client.get("/admin/workflow_materialisation_diagnostics")

    assert response.status_code == 200
    assert response.get_json() == {"success": True}
    assert len(calls) == 1
