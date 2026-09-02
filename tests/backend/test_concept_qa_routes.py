from __future__ import annotations

from flask import Flask


def _app():
    from src.backend.server.routes.concept_routes import concept_bp

    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="concept-qa-route-test")
    app.register_blueprint(concept_bp, url_prefix="/api/concepts")
    return app


def test_canonical_concept_qa_routes_are_registered():
    rules = {rule.rule for rule in _app().url_map.iter_rules()}

    assert "/api/concepts/<string:concept_id>/q_and_a" in rules
    assert "/api/concepts/<string:concept_id>/q_and_a/start" in rules
    assert (
        "/api/concepts/<string:concept_id>/q_and_a/sessions/<string:session_id>/turns"
    ) in rules
    assert (
        "/api/concepts/<string:concept_id>/q_and_a/sessions/<string:session_id>/finish"
    ) in rules
    assert (
        "/api/concepts/<string:concept_id>/q_and_a/sessions/<string:session_id>/cancel"
    ) in rules
    assert (
        "/api/concepts/<string:concept_id>/q_and_a/sessions/"
        "<string:session_id>/formalisations/<string:assertion_id>/confirm"
    ) in rules


def test_concept_q_and_a_routes_require_authenticated_actor(monkeypatch):
    from src.backend.server.routes import concept_routes

    monkeypatch.setattr(concept_routes, "_get_trusted_interaction_user", lambda: None)
    monkeypatch.setattr(
        concept_routes.concept_qa_conversation_service,
        "list_concept_qa_conversations",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unauthenticated requests must not reach the service")
        ),
    )

    response = _app().test_client().get("/api/concepts/%23V%23primary_labs/q_and_a")

    assert response.status_code == 401


def test_confirm_route_preserves_actionable_authority_denial(monkeypatch):
    from src.backend.security import access_control
    from src.backend.server.routes import concept_routes

    monkeypatch.setattr(
        concept_routes,
        "_get_trusted_interaction_user",
        lambda: "#V#actor",
    )
    monkeypatch.setattr(
        concept_routes,
        "_get_request_namespace",
        lambda: "#V#actor@org",
    )
    monkeypatch.setattr(
        access_control,
        "get_effective_organisation_concept_id",
        lambda: "#V#org",
    )
    denial = {
        "success": False,
        "status": "tentative",
        "effect_status": "not_started",
        "assertion_id": "ska_denied",
        "confirmation": {
            "error_code": "organisation_membership_management_authority_required",
            "authority_decision": {"allowed": False},
            "actionable_denial": {
                "who_can_confirm": "an organisation admin or owner",
                "tentative_assertion_preserved": True,
            },
        },
        "receipts": {"formalisation": {"status": "tentative"}},
    }
    monkeypatch.setattr(
        concept_routes.concept_qa_conversation_service,
        "confirm_concept_qa_formalisation",
        lambda **_kwargs: denial,
    )

    response = (
        _app()
        .test_client()
        .post(
            "/api/concepts/%23V%23org/q_and_a/sessions/session-1/"
            "formalisations/ska_denied/confirm",
            json={"request_id": "confirm-1"},
        )
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["status"] == "tentative"
    assert (
        payload["confirmation"]["actionable_denial"]["tentative_assertion_preserved"]
        is True
    )
