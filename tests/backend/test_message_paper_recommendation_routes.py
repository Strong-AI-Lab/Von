from __future__ import annotations

from flask import Flask

import src.backend.server.routes.message_routes as message_routes


def _make_message_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(message_routes.message_bp, url_prefix="/api/messages")
    return app


def test_get_message_paper_recommendation_review_returns_materialised_payload(
    monkeypatch,
):
    app = _make_message_app()
    monkeypatch.setattr(
        message_routes,
        "_get_current_user_concept_id",
        lambda: "#V#lu_yunli",
    )
    monkeypatch.setattr(
        message_routes,
        "get_message",
        lambda _message_id: {
            "concept_id": "#V#message_1",
            "concept_data": {
                "metadata": {
                    "delivery_channel": "paper_recommendation_message",
                    "recommendation_subject_concept_id": "#V#lu_yunli",
                    "recommendation_assertion_ids": [
                        "#V#assertion_1",
                        "#V#assertion_2",
                    ],
                }
            },
        },
    )

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        message_routes,
        "build_message_linked_paper_recommendation_review",
        lambda **kwargs: captured.update(kwargs)
        or {
            "success": True,
            "review_surface_id": "message_panel.paper_recommendation_review.v1",
            "recommendation_report": {
                "success": True,
                "results": [
                    {
                        "assertion_concept_id": "#V#assertion_1",
                        "paper_title": "Causal Models for Scientific Discovery",
                    }
                ],
            },
        },
    )

    with app.test_client() as client:
        resp = client.get(
            "/api/messages/%23V%23message_1/paper_recommendation_review"
        )

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload["success"] is True
    assert payload["recommendation_report"]["results"][0]["paper_title"] == (
        "Causal Models for Scientific Discovery"
    )
    assert captured == {
        "subject_concept_id": "#V#lu_yunli",
        "assertion_concept_ids": ["#V#assertion_1", "#V#assertion_2"],
        "message_concept_id": "#V#message_1",
        "trigger_source": "message_opened",
    }


def test_post_message_paper_recommendation_feedback_records_feedback(monkeypatch):
    app = _make_message_app()
    monkeypatch.setattr(
        message_routes,
        "_get_current_user_concept_id",
        lambda: "#V#lu_yunli",
    )
    monkeypatch.setattr(
        message_routes,
        "_get_current_org_concept_id",
        lambda _user_id: "#V#strong_ai_lab",
    )
    monkeypatch.setattr(
        message_routes,
        "get_message",
        lambda _message_id: {
            "concept_id": "#V#message_1",
            "relationships": {"#V#has_recipient": ["#V#lu_yunli"]},
            "concept_data": {
                "metadata": {
                    "delivery_channel": "paper_recommendation_message",
                    "recommendation_subject_concept_id": "#V#lu_yunli",
                    "recommendation_assertion_ids": ["#V#assertion_1"],
                }
            },
        },
    )

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        message_routes,
        "record_paper_recommendation_feedback",
        lambda **kwargs: captured.update(kwargs)
        or {
            "success": True,
            "assertion_concept_id": kwargs["assertion_concept_id"],
            "feedback_concept_id": "#V#feedback_1",
        },
    )

    with app.test_client() as client:
        resp = client.post(
            "/api/messages/%23V%23message_1/paper_recommendation_feedback",
            json={
                "recommendation_usefulness": "useful",
                "explanation_usefulness": "partly_useful",
                "feedback_text": "The explanation matched my current project.",
            },
        )

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload["success"] is True
    assert captured == {
        "actor_user_concept_id": "#V#lu_yunli",
        "subject_concept_id": "#V#lu_yunli",
        "assertion_concept_id": "#V#assertion_1",
        "paper_concept_id": None,
        "recommendation_usefulness": "useful",
        "explanation_usefulness": "partly_useful",
        "feedback_text": "The explanation matched my current project.",
        "capture_surface": "message_panel.paper_recommendation",
        "organisation_concept_id": "#V#strong_ai_lab",
    }


def test_post_message_paper_recommendation_feedback_rejects_unlinked_assertion(
    monkeypatch,
):
    app = _make_message_app()
    monkeypatch.setattr(
        message_routes,
        "_get_current_user_concept_id",
        lambda: "#V#lu_yunli",
    )
    monkeypatch.setattr(
        message_routes,
        "get_message",
        lambda _message_id: {
            "concept_id": "#V#message_1",
            "concept_data": {
                "metadata": {
                    "delivery_channel": "paper_recommendation_message",
                    "recommendation_assertion_ids": ["#V#assertion_1"],
                }
            },
        },
    )

    with app.test_client() as client:
        resp = client.post(
            "/api/messages/%23V%23message_1/paper_recommendation_feedback",
            json={"assertion_concept_id": "#V#assertion_other", "feedback_text": "No"},
        )

    assert resp.status_code == 400
    assert resp.get_json()["error"] == (
        "assertion_concept_id is not linked to this message"
    )
