from __future__ import annotations

from flask import Flask

from src.backend.server.routes.concept_routes import concept_bp
from src.backend.services.concept_service import ConceptNotFoundError


def _make_concept_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(concept_bp, url_prefix="/api/concepts")
    return app


def test_get_concept_summary_renderer_returns_payload(monkeypatch) -> None:
    app = _make_concept_app()
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.load_concept_summary_renderer",
        lambda concept_id, **_kwargs: {
            "success": True,
            "concept_id": concept_id,
            "selected_renderer": {"renderer_id": "#V#concept_page_document_renderer"},
            "panel": {"variant": "document", "title": "Example paper"},
        },
    )

    with app.test_client() as client:
        resp = client.get("/api/concepts/%23V%23paper_1/summary_renderer")

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload["selected_renderer"]["renderer_id"] == "#V#concept_page_document_renderer"
    assert payload["panel"]["variant"] == "document"


def test_get_concept_summary_renderer_returns_404_for_missing_concept(monkeypatch) -> None:
    app = _make_concept_app()
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.load_concept_summary_renderer",
        lambda _concept_id, **_kwargs: (_ for _ in ()).throw(
            ConceptNotFoundError("missing")
        ),
    )

    with app.test_client() as client:
        resp = client.get("/api/concepts/%23V%23missing/summary_renderer")

    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Concept not found"}


def test_get_concept_summary_renderer_passes_trusted_actor_context(monkeypatch) -> None:
    app = _make_concept_app()
    captured = {}

    def _load(concept_id, **kwargs):
        captured.update({"concept_id": concept_id, **kwargs})
        return {
            "success": True,
            "concept_id": concept_id,
            "panel": {"variant": "generic", "title": "Visible concept"},
        }

    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.load_concept_summary_renderer",
        _load,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._get_current_user_concept_id",
        lambda: "#V#actor",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._get_request_namespace",
        lambda: "#V#actor@lab",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._get_current_org_concept_id",
        lambda: "#V#lab",
    )

    with app.test_client() as client:
        resp = client.get("/api/concepts/%23V%23visible/summary_renderer")

    assert resp.status_code == 200
    assert captured == {
        "concept_id": "#V#visible",
        "actor_user_id": "#V#actor",
        "actor_namespace": "#V#actor@lab",
        "organisation_concept_id": "#V#lab",
    }


def test_get_concept_summary_renderer_passes_no_private_scope_for_anonymous_request(
    monkeypatch,
) -> None:
    app = _make_concept_app()
    captured = {}

    def _load(concept_id, **kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "concept_id": concept_id,
            "panel": {"variant": "generic", "title": "Public concept"},
        }

    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.load_concept_summary_renderer",
        _load,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._get_current_user_concept_id",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._get_request_namespace",
        lambda: (_ for _ in ()).throw(
            AssertionError("anonymous renderer must not resolve a private namespace")
        ),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._get_current_org_concept_id",
        lambda: (_ for _ in ()).throw(
            AssertionError("anonymous renderer must not resolve an organisation")
        ),
    )

    with app.test_client() as client:
        resp = client.get("/api/concepts/%23V%23public/summary_renderer")

    assert resp.status_code == 200
    assert captured == {
        "actor_user_id": None,
        "actor_namespace": None,
        "organisation_concept_id": None,
    }
