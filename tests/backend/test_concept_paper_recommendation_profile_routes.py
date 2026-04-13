from __future__ import annotations

from flask import Flask

from src.backend.server.routes.concept_routes import concept_bp


def _make_concept_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(concept_bp, url_prefix="/api/concepts")
    return app


def test_get_concept_recommendation_profile_returns_payload_with_read_only_permissions(
    monkeypatch,
):
    app = _make_concept_app()

    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.load_paper_recommendation_profile",
        lambda **_kwargs: {
            "success": True,
            "subject_concept_id": "#V#lu_yunli",
            "profile_concept_id": "#V#paper_recommendation_profile_for_lu_yunli",
            "profile": {"project_description": "Graph reasoning"},
            "derived_context": {
                "research_interest_concepts": [
                    {"concept_id": "#V#knowledge_graph", "name": "Knowledge Graph"}
                ]
            },
            "diagnostics": {"profile_exists": True},
        },
    )

    with app.test_client() as client:
        resp = client.get(
            "/api/concepts/%23V%23lu_yunli/paper_recommendation_profile"
        )

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload["profile"]["project_description"] == "Graph reasoning"
    assert payload["permissions"] == {"can_edit": False}


def test_post_concept_recommendation_profile_forbidden_for_different_non_admin_session(
    monkeypatch,
):
    app = _make_concept_app()

    called = {"hit": False}

    def _fake_upsert(**_kwargs):
        called["hit"] = True
        return {"success": True}

    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.upsert_paper_recommendation_profile",
        _fake_upsert,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#someone_else"
            sess["role_in_org"] = "member"

        resp = client.post(
            "/api/concepts/%23V%23lu_yunli/paper_recommendation_profile",
            json={"project_description": "Graph reasoning"},
        )

    assert resp.status_code == 403
    assert called["hit"] is False


def test_post_concept_recommendation_profile_allows_current_org_context(monkeypatch):
    app = _make_concept_app()

    captured: dict[str, object] = {}
    refresh_captured: dict[str, object] = {}

    def _fake_upsert(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "subject_concept_id": kwargs["subject_concept_id"],
            "profile": kwargs["recommendation_profile"],
        }

    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.upsert_paper_recommendation_profile",
        _fake_upsert,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.request_paper_recommendation_refresh",
        lambda **kwargs: refresh_captured.update(kwargs)
        or {"success": True, "triggered": True},
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#lu_yunli"
            sess["organisation_concept_id"] = "#V#strong_ai_lab"

        resp = client.post(
            "/api/concepts/%23V%23strong_ai_lab/paper_recommendation_profile",
            json={
                "project_description": "Organisation-level neuro-symbolic research",
                "stated_interest_terms": ["knowledge graphs"],
            },
        )

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload["success"] is True
    assert payload["permissions"] == {"can_edit": True}
    assert captured["subject_concept_id"] == "#V#strong_ai_lab"
    assert captured["recommendation_profile"] == {
        "project_description": "Organisation-level neuro-symbolic research",
        "stated_interest_terms": ["knowledge graphs"],
    }
    assert refresh_captured["target_subject_concept_ids"] == ["#V#strong_ai_lab"]
    assert refresh_captured["trigger_source"] == (
        "concept_routes.paper_recommendation_profile"
    )
