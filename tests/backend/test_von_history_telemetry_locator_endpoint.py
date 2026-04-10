from __future__ import annotations

from flask import Flask


def test_history_telemetry_locator_uses_org_hint_to_upgrade_bare_namespace(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#test_user"},
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: ("#V#test_user", None),
    )
    monkeypatch.setattr(
        "src.backend.services.conversation_telemetry_locator_service.build_conversation_llm_telemetry_locator",
        lambda **kwargs: (
            captured.update(kwargs)
            or {
                "schema_version": "conversation_llm_telemetry_locator.v1",
                "generated_at_utc": "2026-04-06T03:00:00Z",
                "session_id": kwargs["session_id"],
                "session_name": "Session 1741",
                "namespace_context": {
                    "namespace": kwargs.get("namespace"),
                    "user_id": kwargs.get("user_id"),
                    "org_id": kwargs.get("organisation_concept_id"),
                },
                "metadata": {"total_turns": 0},
                "mcp_access": {},
                "turns": [],
            }
        ),
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")

    client = app.test_client()
    response = client.get(
        "/von/history/telemetry_locator",
        query_string={
            "session_id": "session-1741",
            "organisation_concept_id": "#V#org",
            "namespace": "#V#test_user",
            "user_concept_id": "#V#test_user",
        },
        headers={"X-Von-Window-Session": "ws-1741"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["history_owner_user_id"] == "#V#test_user"
    assert body["requested_user_id"] == "#V#test_user"
    assert captured["namespace"] == "#V#test_user@org"
    assert captured["requested_namespace"] == "#V#test_user@org"
    assert captured["requested_user_id"] == "#V#test_user"
    assert captured["organisation_concept_id"] == "#V#org"
