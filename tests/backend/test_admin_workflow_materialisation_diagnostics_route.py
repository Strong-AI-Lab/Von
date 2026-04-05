from __future__ import annotations

from flask import Flask

import src.backend.server.routes.admin_routes as admin_routes
from src.backend.server.routes.admin_routes import admin_bp


def _build_test_client():
    app = Flask(__name__)
    app.config["TESTING"] = True
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

    response = client.get(
        "/admin/workflow_materialisation_diagnostics"
        "?required_concept_id=%23V%23ephemeral_theory"
        "&required_concept_ids=%23V%23experiment_spec,%23V%23experiment_run"
        "&include_present_concepts=false"
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
