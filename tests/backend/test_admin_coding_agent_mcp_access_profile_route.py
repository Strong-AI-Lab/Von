from __future__ import annotations

from flask import Flask

import src.backend.server.routes.admin_routes as admin_routes
from src.backend.server.routes.admin_routes import admin_bp


def test_coding_agent_mcp_access_profile_route_returns_service_payload(
    monkeypatch,
) -> None:
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(admin_bp)
    client = app.test_client()

    monkeypatch.setattr(
        admin_routes,
        "build_coding_agent_mcp_access_profile",
        lambda: {
            "success": True,
            "profile_id": "coding_agent_vontology_mcp_access",
            "environment": {"authority_state": "test_isolated"},
        },
    )

    response = client.get("/admin/coding_agent_mcp_access_profile")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload == {
        "success": True,
        "profile_id": "coding_agent_vontology_mcp_access",
        "environment": {"authority_state": "test_isolated"},
    }
