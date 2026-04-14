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
        lambda concept_id: {
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
        lambda _concept_id: (_ for _ in ()).throw(ConceptNotFoundError("missing")),
    )

    with app.test_client() as client:
        resp = client.get("/api/concepts/%23V%23missing/summary_renderer")

    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Concept not found"}
