from __future__ import annotations

from flask import Flask

from src.backend.server.routes.settings_routes import settings_bp


def _make_settings_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(settings_bp, url_prefix="/api/settings")
    return app


def test_get_recommendation_profile_returns_payload(monkeypatch):
    app = _make_settings_app()

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.load_paper_recommendation_profile",
        lambda **_kwargs: {
            "success": True,
            "user_concept_id": "#V#lu_yunli",
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
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#lu_yunli"
        resp = client.get("/api/settings/recommendation_profile/%23V%23lu_yunli")

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload["profile"]["project_description"] == "Graph reasoning"
    assert payload["derived_context"]["research_interest_concepts"][0]["name"] == (
        "Knowledge Graph"
    )


def test_get_recommendation_profile_requires_authenticated_actor(monkeypatch):
    app = _make_settings_app()
    called = {"hit": False}

    def _fake_load(**_kwargs):
        called["hit"] = True
        return {"success": True}

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.load_paper_recommendation_profile",
        _fake_load,
    )

    with app.test_client() as client:
        resp = client.get("/api/settings/recommendation_profile/%23V%23lu_yunli")

    assert resp.status_code == 403
    assert called["hit"] is False


def test_post_recommendation_profile_requires_authenticated_actor(monkeypatch):
    app = _make_settings_app()
    called = {"hit": False}

    def _fake_upsert(**_kwargs):
        called["hit"] = True
        return {"success": True}

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.upsert_paper_recommendation_profile",
        _fake_upsert,
    )

    with app.test_client() as client:
        resp = client.post(
            "/api/settings/recommendation_profile/%23V%23lu_yunli",
            json={"project_description": "Graph reasoning"},
        )

    assert resp.status_code == 403
    assert called["hit"] is False


def test_post_recommendation_profile_forbidden_for_different_non_admin_session(
    monkeypatch,
):
    app = _make_settings_app()

    called = {"hit": False}

    def _fake_upsert(**_kwargs):
        called["hit"] = True
        return {"success": True}

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.upsert_paper_recommendation_profile",
        _fake_upsert,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#someone_else"
            sess["role_in_org"] = "member"

        resp = client.post(
            "/api/settings/recommendation_profile/%23V%23lu_yunli",
            json={"project_description": "Graph reasoning"},
        )

    assert resp.status_code == 403
    assert called["hit"] is False


def test_post_recommendation_profile_persists_payload(monkeypatch):
    app = _make_settings_app()

    captured: dict[str, object] = {}
    refresh_captured: dict[str, object] = {}

    def _fake_upsert(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "user_concept_id": kwargs["user_concept_id"],
            "profile": kwargs["recommendation_profile"],
        }

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.upsert_paper_recommendation_profile",
        _fake_upsert,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.request_paper_recommendation_refresh",
        lambda **kwargs: refresh_captured.update(kwargs)
        or {"success": True, "triggered": True},
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#lu_yunli"

        resp = client.post(
            "/api/settings/recommendation_profile/%23V%23lu_yunli",
            json={
                "project_description": "Graph reasoning",
                "stated_interest_terms": ["knowledge graphs"],
            },
        )

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload["success"] is True
    assert captured["user_concept_id"] == "#V#lu_yunli"
    assert captured["recommendation_profile"] == {
        "project_description": "Graph reasoning",
        "stated_interest_terms": ["knowledge graphs"],
    }
    assert payload["recommendation_refresh"] == {"success": True, "triggered": True}
    assert refresh_captured["target_subject_concept_ids"] == ["#V#lu_yunli"]
    assert refresh_captured["trigger_source"] == (
        "settings_routes.recommendation_profile"
    )


def test_post_recommendation_review_returns_payload(monkeypatch):
    app = _make_settings_app()

    captured: dict[str, object] = {}

    def _fake_build_review(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "review_surface_id": "settings.paper_recommendation_review.v1",
            "recommendation_report": {
                "success": True,
                "results": [
                    {
                        "paper_concept_id": "#V#paper_causal_science",
                        "paper_title": "Causal Models for Scientific Discovery",
                    }
                ],
            },
        }

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.build_paper_recommendation_review",
        _fake_build_review,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#lu_yunli"

        resp = client.post(
            "/api/settings/recommendation_review/%23V%23lu_yunli",
            json={
                "candidate_paper_concept_ids": ["#V#paper_causal_science"],
                "candidate_limit": 15,
                "include_all_candidates": True,
                "trigger_source": "manual_review",
            },
        )

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload["success"] is True
    assert payload["recommendation_report"]["results"][0]["paper_title"] == (
        "Causal Models for Scientific Discovery"
    )
    assert captured == {
        "user_concept_id": "#V#lu_yunli",
        "candidate_paper_concept_ids": ["#V#paper_causal_science"],
        "candidate_limit": 15,
        "include_all_candidates": True,
        "trigger_source": "manual_review",
    }


def test_post_recommendation_review_requires_authenticated_actor(monkeypatch):
    app = _make_settings_app()
    called = {"hit": False}

    def _fake_build_review(**_kwargs):
        called["hit"] = True
        return {"success": True}

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.build_paper_recommendation_review",
        _fake_build_review,
    )

    with app.test_client() as client:
        resp = client.post(
            "/api/settings/recommendation_review/%23V%23lu_yunli",
            json={},
        )

    assert resp.status_code == 403
    assert called["hit"] is False


def test_post_recommendation_review_forbidden_for_different_non_admin_session(
    monkeypatch,
):
    app = _make_settings_app()

    called = {"hit": False}

    def _fake_build_review(**_kwargs):
        called["hit"] = True
        return {"success": True}

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.build_paper_recommendation_review",
        _fake_build_review,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#someone_else"
            sess["role_in_org"] = "member"

        resp = client.post(
            "/api/settings/recommendation_review/%23V%23lu_yunli",
            json={},
        )

    assert resp.status_code == 403
    assert called["hit"] is False
