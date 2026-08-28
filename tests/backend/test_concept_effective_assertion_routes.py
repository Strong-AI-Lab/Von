from __future__ import annotations

from flask import Flask

from src.backend.server.routes.concept_routes import concept_bp


def _make_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(concept_bp, url_prefix="/api/concepts")
    return app


def test_anonymous_effective_assertion_route_returns_base_only_without_lookup(
    monkeypatch,
) -> None:
    app = _make_app()
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._get_current_user_concept_id",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.load_concept_effective_assertions",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("anonymous requests must not inspect scoped assertions")
        ),
    )

    with app.test_client() as client:
        response = client.get(
            "/api/concepts/%23V%23public/effective_assertions"
            "?user_concept_id=%23V%23other&organisation_concept_id=%23V%23secret"
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["context_view"] == "base_publication"
    assert payload["items"] == []
    assert payload["has_more"] is False


def test_authenticated_effective_assertion_route_uses_bounded_query_only(
    monkeypatch,
) -> None:
    app = _make_app()
    captured = {}
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._get_current_user_concept_id",
        lambda: "#V#actor",
    )

    def _load(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "concept_id": kwargs["concept_id"],
            "context_view": "actor_effective",
            "items": [{"assertion_id": "ska_visible"}],
            "returned": 1,
            "offset": int(kwargs["offset"]),
            "limit": int(kwargs["limit"]),
            "has_more": False,
            "next_offset": None,
            "truncated": False,
            "counts_are_lower_bounds": False,
        }

    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.load_concept_effective_assertions",
        _load,
    )

    with app.test_client() as client:
        response = client.get(
            "/api/concepts/%23V%23event/effective_assertions"
            "?limit=12&offset=24&user_concept_id=%23V%23other"
        )

    assert response.status_code == 200
    assert captured == {
        "concept_id": "#V#event",
        "limit": "12",
        "offset": "24",
    }
    assert response.get_json()["items"] == [{"assertion_id": "ska_visible"}]


def test_effective_assertion_route_returns_retryable_degraded_state(
    monkeypatch,
) -> None:
    app = _make_app()
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._get_current_user_concept_id",
        lambda: "#V#actor",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.load_concept_effective_assertions",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("store unavailable")),
    )

    with app.test_client() as client:
        response = client.get("/api/concepts/%23V%23event/effective_assertions")

    assert response.status_code == 503
    assert response.get_json() == {
        "error": "Actor-effective assertions are temporarily unavailable",
        "error_code": "actor_effective_assertions_unavailable",
        "retryable": True,
    }


def test_exact_assertion_route_is_non_disclosing_for_anonymous_and_missing(
    monkeypatch,
) -> None:
    app = _make_app()
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._get_current_user_concept_id",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.load_effective_assertion_by_id",
        lambda _assertion_id: (_ for _ in ()).throw(
            AssertionError("anonymous requests must not inspect assertions")
        ),
    )

    with app.test_client() as client:
        anonymous = client.get("/api/concepts/assertions/ska_private")

    assert anonymous.status_code == 404
    assert anonymous.get_json() == {"error": "assertion_not_found"}

    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._get_current_user_concept_id",
        lambda: "#V#actor",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.load_effective_assertion_by_id",
        lambda _assertion_id: None,
    )
    with app.test_client() as client:
        missing = client.get("/api/concepts/assertions/ska_private")

    assert missing.status_code == 404
    assert missing.get_json() == {"error": "assertion_not_found"}


def test_exact_assertion_route_returns_actor_visible_projection(monkeypatch) -> None:
    app = _make_app()
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes._get_current_user_concept_id",
        lambda: "#V#actor",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.concept_routes.load_effective_assertion_by_id",
        lambda assertion_id: {
            "success": True,
            "context_view": "actor_effective",
            "assertion": {"assertion_id": assertion_id, "status": "retracted"},
        },
    )

    with app.test_client() as client:
        response = client.get("/api/concepts/assertions/ska_visible")

    assert response.status_code == 200
    assert response.get_json()["assertion"] == {
        "assertion_id": "ska_visible",
        "status": "retracted",
    }
