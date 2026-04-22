from __future__ import annotations

from typing import Any

import pytest
from flask import Flask

from src.backend.server.routes.settings_routes import settings_bp
from src.backend.services import settings_service

@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, True),
        ("", True),
        ("false", False),
        ("0", False),
        (0, False),
        ("true", True),
        ("1", True),
        (1, True),
        (True, True),
    ],
)
def test_get_disable_write_tool_conservatism_coerces(
    raw: Any, expected: bool, monkeypatch
):
    monkeypatch.setattr(settings_service, "get_setting", lambda _name: raw)
    assert settings_service.get_disable_write_tool_conservatism() is expected


def test_get_global_mutation_authority_level_reflects_legacy_admin_setting(
    monkeypatch,
):
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: True
    )
    assert (
        settings_service.get_global_mutation_authority_level()
        == "external_system_guarded"
    )

    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: False
    )
    assert (
        settings_service.get_global_mutation_authority_level()
        == "external_system_guarded"
    )


def _make_settings_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(settings_bp, url_prefix="/api/settings")
    return app


def test_settings_endpoint_rejects_disable_write_conservatism_for_non_admin(
    monkeypatch,
):
    app = _make_settings_app()

    called = {"hit": False}

    def _setter(_disabled: bool) -> bool:
        called["hit"] = True
        return True

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_disable_write_tool_conservatism",
        _setter,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["role_in_org"] = "member"

        resp = client.post(
            "/api/settings/",
            json={"disable_write_tool_conservatism": True},
        )

        assert resp.status_code == 403
        assert called["hit"] is False


def test_settings_endpoint_allows_disable_write_conservatism_for_admin(monkeypatch):
    app = _make_settings_app()

    captured: dict[str, Any] = {}

    def _setter(disabled: bool) -> bool:
        captured["disabled"] = disabled
        return True

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_disable_write_tool_conservatism",
        _setter,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["role_in_org"] = "admin"

        resp = client.post(
            "/api/settings/",
            json={"disable_write_tool_conservatism": True},
        )

        assert resp.status_code == 200
        assert captured["disabled"] is True


def test_settings_endpoint_allows_disable_write_conservatism_for_window_session_admin(
    monkeypatch,
):
    app = _make_settings_app()

    captured: dict[str, Any] = {}

    def _setter(disabled: bool) -> bool:
        captured["disabled"] = disabled
        return True

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_disable_write_tool_conservatism",
        _setter,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_effective_context",
        lambda window_session_id, flask_session, user_id: {
            "role": "admin",
            "organisation_id": "#V#uoa_strong_ai_lab",
            "namespace": "#V#michael_witbrock@uoa_strong_ai_lab",
        },
    )

    with app.test_client() as client:
        resp = client.post(
            "/api/settings/",
            json={"disable_write_tool_conservatism": True},
            headers={"X-Von-Window-Session": "window-123"},
        )

        assert resp.status_code == 200
        assert captured["disabled"] is True


def test_settings_endpoint_rejects_disable_write_conservatism_for_window_session_member(
    monkeypatch,
):
    app = _make_settings_app()

    called = {"hit": False}

    def _setter(_disabled: bool) -> bool:
        called["hit"] = True
        return True

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_disable_write_tool_conservatism",
        _setter,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_effective_context",
        lambda window_session_id, flask_session, user_id: {
            "role": "member",
            "organisation_id": "#V#uoa_strong_ai_lab",
            "namespace": "#V#michael_witbrock@uoa_strong_ai_lab",
        },
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["role_in_org"] = "admin"

        resp = client.post(
            "/api/settings/",
            json={"disable_write_tool_conservatism": True},
            headers={"X-Von-Window-Session": "window-123"},
        )

        assert resp.status_code == 403
        assert called["hit"] is False


def test_settings_endpoint_only_returns_flag_for_admin(monkeypatch):
    app = _make_settings_app()

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_disable_write_tool_conservatism",
        lambda: True,
    )

    with app.test_client() as client:
        # Non-admin: field should be absent
        with client.session_transaction() as sess:
            sess["role_in_org"] = "member"

        resp = client.get("/api/settings/")
        assert resp.status_code == 200
        payload = resp.get_json() or {}
        assert "disable_write_tool_conservatism" not in payload

        # Admin: field should be present
        with client.session_transaction() as sess:
            sess["role_in_org"] = "owner"

        resp2 = client.get("/api/settings/")
        assert resp2.status_code == 200
        payload2 = resp2.get_json() or {}
        assert payload2.get("disable_write_tool_conservatism") is True


def test_settings_endpoint_updates_auto_proceed_minimal_imposition(monkeypatch):
    app = _make_settings_app()

    captured: dict[str, Any] = {}

    def _setter(enabled: bool) -> bool:
        captured["enabled"] = enabled
        return True

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_auto_proceed_minimal_imposition_enabled",
        _setter,
    )

    with app.test_client() as client:
        resp = client.post(
            "/api/settings/",
            json={"auto_proceed_minimal_imposition_enabled": False},
        )

        assert resp.status_code == 200
        assert captured["enabled"] is False


def test_settings_endpoint_invalidates_workflow_capability_index_when_embedder_changes(
    monkeypatch,
):
    app = _make_settings_app()

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_server_default_llm_setting",
        lambda payload: payload == {"provider": "ollama", "model": "gemma4:26b"},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_rag_embedder_setting",
        lambda payload: payload
        == {"mode": "explicit", "provider": "openai", "model": "text-embedding-3-small"},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_rag_llm_setting",
        lambda payload: payload == {"mode": "disabled"},
    )

    embedder_resolutions = iter(
        [
            {
                "status": "resolved",
                "effective": {"provider": "ollama", "model": "nomic-embed-text"},
                "selection_source": "server_default_llm",
                "reason": None,
            },
            {
                "status": "resolved",
                "effective": {
                    "provider": "openai",
                    "model": "text-embedding-3-small",
                },
                "selection_source": "explicit_setting",
                "reason": None,
            },
        ]
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_rag_embedder_setting",
        lambda *args, **kwargs: next(embedder_resolutions),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_rag_llm_setting",
        lambda *args, **kwargs: {
            "status": "disabled",
            "effective": None,
            "selection_source": "configured_disabled",
            "reason": "disabled_by_setting",
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_server_default_llm_setting",
        lambda: {"provider": "ollama", "model": "gemma4:26b"},
    )

    invalidation_calls: list[dict[str, Any]] = []
    prewarm_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.invalidate_workflow_capability_index",
        lambda **kwargs: invalidation_calls.append(dict(kwargs))
        or {"success": True, "backend_namespace_reset": True, "reason": kwargs.get("reason")},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.prewarm_workflow_capability_index",
        lambda **kwargs: prewarm_calls.append(dict(kwargs)) or True,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_workflow_capability_index_readiness_report",
        lambda: {
            "status": "building",
            "warning_level": "warning",
            "summary": "Workflow capability index still building.",
            "detail": "Workflow discovery is waiting on the authoritative capability index to finish building.",
        },
    )

    with app.test_client() as client:
        resp = client.post(
            "/api/settings/",
            json={
                "server_default_llm": {"provider": "ollama", "model": "gemma4:26b"},
                "rag_embedder": {
                    "mode": "explicit",
                    "provider": "openai",
                    "model": "text-embedding-3-small",
                },
                "rag_llm": {"mode": "disabled"},
            },
        )

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload["workflow_capability_rebuild"]["required"] is True
    assert invalidation_calls == [
        {
            "reason": payload["workflow_capability_rebuild"]["detail"],
            "reset_backend_namespace": True,
        }
    ]
    assert prewarm_calls == [{"force_refresh": True}]
    assert payload["workflow_capability_index"]["status"] == "building"


def test_settings_endpoint_rejects_active_llm_without_scope(monkeypatch):
    app = _make_settings_app()

    user_called = {"hit": False}
    org_called = {"hit": False}

    def _set_user_llm_setting(*_args, **_kwargs) -> bool:
        user_called["hit"] = True
        return True

    def _set_org_llm_setting(*_args, **_kwargs) -> bool:
        org_called["hit"] = True
        return True

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_user_llm_setting",
        _set_user_llm_setting,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_org_llm_setting",
        _set_org_llm_setting,
    )

    with app.test_client() as client:
        resp = client.post(
            "/api/settings/",
            json={"active_llm": {"provider": "openai", "model": "gpt-5-mini"}},
        )

    assert resp.status_code == 400
    payload = resp.get_json() or {}
    assert payload.get("message") == (
        "Model changes require a current user or organisation context."
    )
    assert user_called["hit"] is False
    assert org_called["hit"] is False


def test_settings_endpoint_returns_resolved_llm_after_scoped_save(monkeypatch):
    app = _make_settings_app()

    captured: dict[str, Any] = {}

    def _set_user_llm_setting(concept_id: str, provider: str, model: str) -> bool:
        captured["concept_id"] = concept_id
        captured["provider"] = provider
        captured["model"] = model
        return True

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_user_llm_setting",
        _set_user_llm_setting,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_llm_setting",
        lambda user_concept_id=None, org_concept_id=None: {
            "provider": "openai",
            "model": "gpt-5-mini",
            "scope": "user",
            "user_concept_id": user_concept_id,
            "organisation_concept_id": org_concept_id,
        },
    )

    with app.test_client() as client:
        resp = client.post(
            "/api/settings/",
            json={
                "active_llm": {
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "scope": "user",
                    "concept_id": "#V#michael_witbrock",
                }
            },
        )

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("message") == "Settings updated successfully."
    assert payload.get("resolved_llm", {}).get("model") == "gpt-5-mini"
    assert payload.get("resolved_llm", {}).get("scope") == "user"
    assert captured == {
        "concept_id": "#V#michael_witbrock",
        "provider": "openai",
        "model": "gpt-5-mini",
    }


def test_settings_get_marks_response_no_store(monkeypatch):
    app = _make_settings_app()

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_all_settings_data",
        lambda: {"openai_api_key_env_var": "OPENAI_API_KEY"},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_llm_setting",
        lambda user_concept_id=None, org_concept_id=None: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_enabled_llm_settings",
        lambda user_concept_id=None, org_concept_id=None: [],
    )

    with app.test_client() as client:
        resp = client.get("/api/settings/")

    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "no-store"


def test_llm_info_marks_response_no_store(monkeypatch):
    app = _make_settings_app()

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_llm_setting",
        lambda user_concept_id=None, org_concept_id=None: None,
    )

    with app.test_client() as client:
        resp = client.get("/api/settings/llm/info")

    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "no-store"


def test_settings_endpoint_saves_user_mutation_authority(monkeypatch):
    app = _make_settings_app()

    captured: dict[str, Any] = {}

    def _setter(concept_id: str, level: str) -> bool:
        captured["concept_id"] = concept_id
        captured["level"] = level
        return True

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_user_mutation_authority_level",
        _setter,
    )

    with app.test_client() as client:
        resp = client.post(
            "/api/settings/",
            json={
                "mutation_authority": {
                    "scope": "user",
                    "concept_id": "#V#michael_witbrock",
                    "level": "mutative_vontology_non_destructive",
                }
            },
        )

    assert resp.status_code == 200
    assert captured == {
        "concept_id": "#V#michael_witbrock",
        "level": "mutative_vontology_non_destructive",
    }


def test_settings_endpoint_returns_resolved_mutation_authority(monkeypatch):
    app = _make_settings_app()

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_global_mutation_authority_level",
        lambda: "external_system_guarded",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_user_mutation_authority_level",
        lambda _concept_id: "additive_vontology",
    )

    with app.test_client() as client:
        resp = client.get("/api/settings/?user_concept_id=%23V%23michael_witbrock")

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    resolved = payload.get("resolved_mutation_authority") or {}
    assert resolved.get("scope") == "user"
    assert resolved.get("concept_id") == "#V#michael_witbrock"
    assert resolved.get("level") == "additive_vontology"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, False),
        ("", False),
        ("false", False),
        ("0", False),
        (0, False),
        ("true", True),
        ("1", True),
        (1, True),
        (True, True),
    ],
)
def test_get_require_human_review_for_high_impact_kb_writes_coerces(
    raw: Any, expected: bool, monkeypatch
):
    monkeypatch.setattr(settings_service, "get_setting", lambda _name: raw)
    assert (
        settings_service.get_require_human_review_for_high_impact_kb_writes()
        is expected
    )


def test_settings_endpoint_rejects_high_impact_review_flag_for_non_admin(monkeypatch):
    app = _make_settings_app()

    called = {"hit": False}

    def _setter(_required: bool) -> bool:
        called["hit"] = True
        return True

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_require_human_review_for_high_impact_kb_writes",
        _setter,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["role_in_org"] = "member"

        resp = client.post(
            "/api/settings/",
            json={"require_human_review_for_high_impact_kb_writes": True},
        )

        assert resp.status_code == 403
        assert called["hit"] is False


def test_settings_endpoint_allows_high_impact_review_flag_for_admin(monkeypatch):
    app = _make_settings_app()

    captured: dict[str, Any] = {}

    def _setter(required: bool) -> bool:
        captured["required"] = required
        return True

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_require_human_review_for_high_impact_kb_writes",
        _setter,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["role_in_org"] = "owner"

        resp = client.post(
            "/api/settings/",
            json={"require_human_review_for_high_impact_kb_writes": True},
        )

        assert resp.status_code == 200
        assert captured["required"] is True


def test_settings_endpoint_only_returns_high_impact_review_flag_for_admin(monkeypatch):
    app = _make_settings_app()

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_require_human_review_for_high_impact_kb_writes",
        lambda: True,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["role_in_org"] = "member"

        resp = client.get("/api/settings/")
        assert resp.status_code == 200
        payload = resp.get_json() or {}
        assert "require_human_review_for_high_impact_kb_writes" not in payload

        with client.session_transaction() as sess:
            sess["role_in_org"] = "admin"

        resp2 = client.get("/api/settings/")
        assert resp2.status_code == 200
        payload2 = resp2.get_json() or {}
        assert payload2.get("require_human_review_for_high_impact_kb_writes") is True
