from __future__ import annotations

from types import SimpleNamespace
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
        == {
            "mode": "explicit",
            "provider": "openai",
            "model": "text-embedding-3-small",
        },
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
    runtime_cache_invalidations: list[str] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.invalidate_workflow_capability_index",
        lambda **kwargs: invalidation_calls.append(dict(kwargs))
        or {
            "success": True,
            "backend_namespace_reset": True,
            "reason": kwargs.get("reason"),
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.prewarm_workflow_capability_index",
        lambda **kwargs: prewarm_calls.append(dict(kwargs)) or True,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes._invalidate_cached_rag_runtime_configuration",
        lambda: runtime_cache_invalidations.append("called"),
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["role_in_org"] = "owner"

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
    assert runtime_cache_invalidations == ["called"]
    assert invalidation_calls == [
        {
            "reason": (
                "The RAG embedder changed, so the authoritative workflow "
                "capability index was invalidated and will rebuild with the "
                "new embedding configuration. Existing embedding-backed "
                "namespaces built with the previous embedder are treated as "
                "incompatible until rebuilt."
            ),
            "reset_backend_namespace": True,
        }
    ]
    assert prewarm_calls == [{"force_refresh": True}]
    assert "workflow_capability_index" not in payload
    assert "workflow_capability_rebuild" not in payload


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

    def _set_user_llm_setting(
        concept_id: str,
        provider: str,
        model: str,
        *,
        host=None,
        enabled_entries=None,
    ) -> bool:
        captured["concept_id"] = concept_id
        captured["provider"] = provider
        captured["model"] = model
        captured["host"] = host
        captured["enabled_entries"] = list(enabled_entries or [])
        return True

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_user_llm_setting",
        _set_user_llm_setting,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_llm_setting",
        lambda user_concept_id=None, org_concept_id=None: {
            "provider": "ollama",
            "model": "gemma4:latest",
            "host": "http://127.0.0.1:11434",
            "scope": "user",
            "user_concept_id": user_concept_id,
            "organisation_concept_id": org_concept_id,
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_enabled_llm_settings",
        lambda user_concept_id=None, org_concept_id=None: [
            {
                "provider": "ollama",
                "model": "gemma4:latest",
                "host": "http://127.0.0.1:11434",
                "scope": "user",
                "user_concept_id": user_concept_id,
            },
            {
                "provider": "openai",
                "model": "gpt-5-mini",
                "scope": "user",
                "user_concept_id": user_concept_id,
            },
        ],
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#michael_witbrock"

        resp = client.post(
            "/api/settings/",
            json={
                "active_llm": {
                    "provider": "ollama",
                    "model": "gemma4:latest",
                    "host": "http://127.0.0.1:11434",
                    "scope": "user",
                    "concept_id": "#V#michael_witbrock",
                },
                "enabled_llms": [
                    {
                        "provider": "ollama",
                        "model": "gemma4:latest",
                        "host": "http://127.0.0.1:11434",
                    },
                    {"provider": "openai", "model": "gpt-5-mini"},
                ],
            },
        )

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("message") == "Settings updated successfully."
    assert payload.get("resolved_llm", {}).get("model") == "gemma4:latest"
    assert payload.get("resolved_llm", {}).get("host") == "http://127.0.0.1:11434"
    assert payload.get("resolved_llm", {}).get("scope") == "user"
    assert payload.get("enabled_llms", [])[1]["model"] == "gpt-5-mini"
    assert captured == {
        "concept_id": "#V#michael_witbrock",
        "provider": "ollama",
        "model": "gemma4:latest",
        "host": "http://127.0.0.1:11434",
        "enabled_entries": [
            {
                "provider": "ollama",
                "model": "gemma4:latest",
                "host": "http://127.0.0.1:11434",
            },
            {"provider": "openai", "model": "gpt-5-mini"},
        ],
    }


def test_settings_endpoint_rejects_cross_user_model_scope_before_other_mutations(
    monkeypatch,
):
    app = _make_settings_app()
    calls: list[str] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_auto_proceed_minimal_imposition_enabled",
        lambda _enabled: calls.append("auto_proceed") or True,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_user_llm_setting",
        lambda *_args, **_kwargs: calls.append("model") or True,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#current_user"

        resp = client.post(
            "/api/settings/",
            json={
                "auto_proceed_minimal_imposition_enabled": False,
                "active_llm": {
                    "provider": "ollama",
                    "model": "gemma4:latest",
                    "scope": "user",
                    "concept_id": "#V#other_user",
                },
            },
        )

    assert resp.status_code == 403
    assert calls == []


def test_settings_endpoint_rejects_legacy_identity_header_for_model_write(
    monkeypatch,
):
    app = _make_settings_app()
    calls: list[str] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_effective_user_concept_id",
        lambda: "#V#claimed_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_user_llm_setting",
        lambda *_args, **_kwargs: calls.append("model") or True,
    )

    with app.test_client() as client:
        resp = client.post(
            "/api/settings/",
            headers={"X-User-Concept-ID": "#V#claimed_user"},
            json={
                "active_llm": {
                    "provider": "ollama",
                    "model": "gemma4:latest",
                    "scope": "user",
                    "concept_id": "#V#claimed_user",
                }
            },
        )

    assert resp.status_code == 403
    assert (resp.get_json() or {}).get("message") == (
        "An authenticated actor is required to update model settings."
    )
    assert calls == []


def test_settings_endpoint_allows_explicit_admin_token_model_automation(monkeypatch):
    app = _make_settings_app()
    captured: dict[str, str] = {}
    monkeypatch.setenv("VON_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_user_llm_setting",
        lambda concept_id, provider, model: captured.update(
            concept_id=concept_id,
            provider=provider,
            model=model,
        )
        is None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_llm_setting",
        lambda user_concept_id=None, org_concept_id=None: {
            "provider": "ollama",
            "model": "gemma4:latest",
            "scope": "user",
            "user_concept_id": user_concept_id,
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_enabled_llm_settings",
        lambda **_kwargs: [],
    )

    with app.test_client() as client:
        resp = client.post(
            "/api/settings/",
            headers={"X-Von-Admin-Token": "test-admin-token"},
            json={
                "active_llm": {
                    "provider": "ollama",
                    "model": "gemma4:latest",
                    "scope": "user",
                    "concept_id": "#V#automation_target",
                }
            },
        )

    assert resp.status_code == 200
    assert captured == {
        "concept_id": "#V#automation_target",
        "provider": "ollama",
        "model": "gemma4:latest",
    }


def test_settings_endpoint_rejects_non_string_model_host_before_mutation(monkeypatch):
    app = _make_settings_app()
    calls: list[str] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_user_llm_setting",
        lambda *_args, **_kwargs: calls.append("model") or True,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#current_user"

        resp = client.post(
            "/api/settings/",
            json={
                "active_llm": {
                    "provider": "ollama",
                    "model": "gemma4:latest",
                    "host": 11434,
                    "scope": "user",
                    "concept_id": "#V#current_user",
                }
            },
        )

    assert resp.status_code == 400
    assert calls == []


@pytest.mark.parametrize(
    ("effective_org", "role"),
    [
        ("#V#other_org", "admin"),
        ("#V#target_org", "member"),
    ],
)
def test_settings_endpoint_rejects_unauthorised_organisation_model_scope(
    monkeypatch,
    effective_org: str,
    role: str,
):
    app = _make_settings_app()
    called: list[str] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_org_llm_setting",
        lambda *_args, **_kwargs: called.append("model") or True,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#current_user"
            sess["organisation_concept_id"] = effective_org
            sess["role_in_org"] = role

        resp = client.post(
            "/api/settings/",
            json={
                "active_llm": {
                    "provider": "ollama",
                    "model": "gemma4:latest",
                    "scope": "organisation",
                    "concept_id": "#V#target_org",
                }
            },
        )

    assert resp.status_code == 403
    assert called == []


def test_settings_endpoint_allows_current_organisation_admin_model_scope(monkeypatch):
    app = _make_settings_app()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_org_llm_setting",
        lambda concept_id, provider, model: captured.update(
            concept_id=concept_id,
            provider=provider,
            model=model,
        )
        is None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_llm_setting",
        lambda user_concept_id=None, org_concept_id=None: {
            "provider": "ollama",
            "model": "gemma4:latest",
            "scope": "organisation",
            "organisation_concept_id": org_concept_id,
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_enabled_llm_settings",
        lambda **_kwargs: [],
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#current_user"
            sess["organisation_concept_id"] = "#V#target_org"
            sess["role_in_org"] = "admin"

        resp = client.post(
            "/api/settings/",
            json={
                "active_llm": {
                    "provider": "ollama",
                    "model": "gemma4:latest",
                    "scope": "organisation",
                    "concept_id": "#V#target_org",
                }
            },
        )

    assert resp.status_code == 200
    assert captured == {
        "concept_id": "#V#target_org",
        "provider": "ollama",
        "model": "gemma4:latest",
    }


def test_settings_endpoint_rejects_shared_model_mutations_for_non_admin_before_writes(
    monkeypatch,
):
    app = _make_settings_app()
    calls: list[str] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_server_default_llm_setting",
        lambda _payload: calls.append("server_default") or True,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_rag_embedder_setting",
        lambda _payload: calls.append("rag_embedder") or True,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_rag_llm_setting",
        lambda _payload: calls.append("rag_llm") or True,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_auto_proceed_minimal_imposition_enabled",
        lambda _enabled: calls.append("auto_proceed") or True,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["role_in_org"] = "member"

        resp = client.post(
            "/api/settings/",
            json={
                "auto_proceed_minimal_imposition_enabled": False,
                "server_default_llm": {
                    "provider": "ollama",
                    "model": "gemma4:latest",
                },
                "rag_embedder": {"mode": "inherit"},
                "rag_llm": {"mode": "disabled"},
            },
        )

    assert resp.status_code == 403
    assert calls == []


def test_settings_get_uses_effective_actor_instead_of_query_claims(monkeypatch):
    app = _make_settings_app()
    resolution_calls: list[tuple[str, str | None, str | None]] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_effective_user_concept_id",
        lambda: "#V#current_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_effective_organisation_concept_id",
        lambda: "#V#current_org",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_llm_setting",
        lambda user_concept_id=None, org_concept_id=None: resolution_calls.append(
            ("primary", user_concept_id, org_concept_id)
        ),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_enabled_llm_settings",
        lambda user_concept_id=None, org_concept_id=None: resolution_calls.append(
            ("enabled", user_concept_id, org_concept_id)
        )
        or [],
    )

    with app.test_client() as client:
        resp = client.get(
            "/api/settings/?user_concept_id=%23V%23other_user"
            "&organisation_concept_id=%23V%23other_org"
        )

    assert resp.status_code == 200
    assert resolution_calls == [
        ("primary", "#V#current_user", "#V#current_org"),
        ("enabled", "#V#current_user", "#V#current_org"),
    ]
    assert "workflow_capability_index" not in (resp.get_json() or {})


def test_settings_get_without_authenticated_actor_ignores_query_scope_claims(
    monkeypatch,
):
    app = _make_settings_app()
    primary_calls: list[tuple[str | None, str | None]] = []
    enabled_calls: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_effective_user_concept_id",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_effective_organisation_concept_id",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_llm_setting",
        lambda user_concept_id=None, org_concept_id=None: primary_calls.append(
            (user_concept_id, org_concept_id)
        ),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_enabled_llm_settings",
        lambda user_concept_id=None, org_concept_id=None: enabled_calls.append(
            (user_concept_id, org_concept_id)
        )
        or [],
    )

    with app.test_client() as client:
        response = client.get(
            "/api/settings/?user_concept_id=%23V%23other_user"
            "&organisation_concept_id=%23V%23other_org"
        )

    assert response.status_code == 200
    payload = response.get_json() or {}
    assert primary_calls == [(None, None)]
    assert enabled_calls == [(None, None)]
    assert payload["resolved_llm"] is None
    assert payload["effective_llm"] is None
    assert payload["enabled_llms"] == []
    assert payload["effective_enabled_llms"] == []
    assert "resolved_mutation_authority" not in payload


@pytest.mark.parametrize(
    ("model_scope", "selected_user", "selected_org", "selected_model"),
    [
        ("user", "#V#current_user", None, "user-model"),
        ("organisation", None, "#V#current_org", "org-model"),
    ],
)
def test_settings_get_resolves_explicit_actor_bound_model_scope_without_fallback(
    monkeypatch,
    model_scope: str,
    selected_user: str | None,
    selected_org: str | None,
    selected_model: str,
):
    app = _make_settings_app()
    primary_calls: list[tuple[str | None, str | None]] = []
    enabled_calls: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_effective_user_concept_id",
        lambda: "#V#current_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_effective_organisation_concept_id",
        lambda: "#V#current_org",
    )

    def _resolve_primary(user_concept_id=None, org_concept_id=None):
        primary_calls.append((user_concept_id, org_concept_id))
        if user_concept_id:
            return {"model": "user-model", "scope": "user"}
        if org_concept_id:
            return {"model": "org-model", "scope": "organisation"}
        return None

    def _resolve_enabled(user_concept_id=None, org_concept_id=None):
        enabled_calls.append((user_concept_id, org_concept_id))
        model = "user-model" if user_concept_id else "org-model"
        return [{"provider": "ollama", "model": model}]

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_llm_setting",
        _resolve_primary,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_enabled_llm_settings",
        _resolve_enabled,
    )

    with app.test_client() as client:
        response = client.get(
            "/api/settings/?model_scope="
            f"{model_scope}&user_concept_id=%23V%23other_user"
            "&organisation_concept_id=%23V%23other_org"
        )

    assert response.status_code == 200
    payload = response.get_json() or {}
    assert payload["selected_model_scope"] == model_scope
    assert payload["resolved_llm"]["model"] == selected_model
    assert payload["effective_llm"]["model"] == "user-model"
    assert payload["enabled_llms"] == [{"provider": "ollama", "model": selected_model}]
    assert payload["effective_enabled_llms"] == [
        {"provider": "ollama", "model": "user-model"}
    ]
    assert primary_calls == [
        ("#V#current_user", "#V#current_org"),
        (selected_user, selected_org),
    ]
    assert enabled_calls == [
        ("#V#current_user", "#V#current_org"),
        (selected_user, selected_org),
    ]


def test_settings_get_rejects_organisation_scope_without_effective_org(monkeypatch):
    app = _make_settings_app()
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_effective_user_concept_id",
        lambda: "#V#current_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_effective_organisation_concept_id",
        lambda: None,
    )

    with app.test_client() as client:
        response = client.get(
            "/api/settings/?model_scope=organisation"
            "&organisation_concept_id=%23V%23other_org"
        )

    assert response.status_code == 403


def test_settings_get_rejects_invalid_model_scope():
    app = _make_settings_app()

    with app.test_client() as client:
        response = client.get("/api/settings/?model_scope=global")

    assert response.status_code == 400


def test_llm_override_rejects_cross_user_scope(monkeypatch):
    app = _make_settings_app()
    called: list[str] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_user_llm_setting",
        lambda *_args, **_kwargs: called.append("model") or True,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#current_user"

        resp = client.post(
            "/api/settings/llm/override",
            json={
                "provider": "ollama",
                "model": "gemma4:latest",
                "scope": "user",
                "concept_id": "#V#other_user",
            },
        )

    assert resp.status_code == 403
    assert called == []


def test_llm_override_rejects_legacy_identity_header_without_session(monkeypatch):
    app = _make_settings_app()
    called: list[str] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_effective_user_concept_id",
        lambda: "#V#claimed_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_user_llm_setting",
        lambda *_args, **_kwargs: called.append("model") or True,
    )

    with app.test_client() as client:
        resp = client.post(
            "/api/settings/llm/override",
            headers={"X-User-Concept-ID": "#V#claimed_user"},
            json={
                "provider": "ollama",
                "model": "gemma4:latest",
                "scope": "user",
                "concept_id": "#V#claimed_user",
            },
        )

    assert resp.status_code == 403
    assert (resp.get_json() or {}).get("message") == (
        "An authenticated actor is required to update model settings."
    )
    assert called == []


def test_settings_endpoint_reports_atomic_model_pool_persistence_failure(monkeypatch):
    app = _make_settings_app()
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_user_llm_setting",
        lambda *_args, **_kwargs: False,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#current_user"

        resp = client.post(
            "/api/settings/",
            json={
                "active_llm": {
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "scope": "user",
                    "concept_id": "#V#current_user",
                },
                "enabled_llms": [
                    {"provider": "openai", "model": "gpt-5-mini"},
                    {"provider": "ollama", "model": "gemma4:latest"},
                ],
            },
        )

    assert resp.status_code == 500
    assert "enabled model pool" in (resp.get_json() or {}).get("message", "")


def test_scoped_model_configuration_persists_primary_and_pool_in_one_document(
    monkeypatch,
):
    user_id = "#V#current_user"
    canonical_name = f"llm_configuration:user:{user_id}"
    primary_name = f"active_llm:user:{user_id}"
    enabled_name = f"enabled_llms:user:{user_id}"
    collection = object()
    monkeypatch.setattr(
        settings_service,
        "get_application_settings_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        settings_service,
        "_read_scoped_llm_configuration",
        lambda **_kwargs: {
            "canonical_exists": False,
            "revision": 0,
            "primary": None,
            "enabled_llms": [{"provider": "ollama", "model": "qwen3:8b"}],
            "source": "legacy",
        },
    )
    writes: list[tuple[Any, dict[str, Any], dict[str, Any], bool]] = []

    def _update_one(coll, query, update, *, upsert, **_kwargs):
        writes.append((coll, query, update, upsert))
        return SimpleNamespace(acknowledged=True, upserted_id="new", matched_count=0)

    monkeypatch.setattr(
        settings_service,
        "_settings_update_one",
        _update_one,
    )

    assert settings_service.set_user_llm_setting(
        user_id,
        "ollama",
        "gemma4:latest",
        host="http://127.0.0.1:11434",
        enabled_entries=[
            {
                "provider": "ollama",
                "model": "gemma4:latest",
                "host": "http://127.0.0.1:11434",
            },
            {"provider": "openai", "model": "gpt-5-mini"},
        ],
    )

    assert len(writes) == 1
    written_collection, query, update, upsert = writes[0]
    assert written_collection is collection
    assert query == {
        "setting_name": canonical_name,
        "revision": {"$exists": False},
    }
    assert upsert is True
    assert update["$set"]["setting_name"] == canonical_name
    assert update["$set"]["revision"] == 1
    assert update["$set"]["value"] == {
        "schema_version": 1,
        "primary": {
            "provider": "ollama",
            "model": "gemma4:latest",
            "host": "http://127.0.0.1:11434",
        },
        "enabled_llms": [
            {
                "provider": "ollama",
                "model": "gemma4:latest",
                "host": "http://127.0.0.1:11434",
            },
            {"provider": "openai", "model": "gpt-5-mini"},
        ],
    }
    assert primary_name not in str(writes)
    assert enabled_name not in str(writes)


def test_org_model_configuration_uses_one_canonical_org_document(monkeypatch):
    org_id = "#V#current_org"
    collection = object()
    monkeypatch.setattr(
        settings_service,
        "get_application_settings_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        settings_service,
        "_read_scoped_llm_configuration",
        lambda **_kwargs: {
            "canonical_exists": False,
            "revision": 0,
            "primary": None,
            "enabled_llms": [],
            "source": "legacy",
        },
    )
    writes: list[tuple[dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(
        settings_service,
        "_settings_update_one",
        lambda _collection, query, update, **_kwargs: (
            writes.append((query, update))
            or SimpleNamespace(
                acknowledged=True,
                upserted_id="new",
                matched_count=0,
            )
        ),
    )

    assert settings_service.set_org_llm_setting(org_id, "ollama", "qwen3:8b")

    assert len(writes) == 1
    assert writes[0][0]["setting_name"] == f"llm_configuration:org:{org_id}"
    assert writes[0][1]["$set"]["value"]["primary"] == {
        "provider": "ollama",
        "model": "qwen3:8b",
    }


def test_scoped_model_configuration_reads_legacy_only_when_canonical_absent(
    monkeypatch,
):
    user_id = "#V#current_user"
    canonical_name = f"llm_configuration:user:{user_id}"
    primary_name = f"active_llm:user:{user_id}"
    enabled_name = f"enabled_llms:user:{user_id}"
    collection = object()
    legacy_reads: list[dict[str, Any]] = []
    monkeypatch.setattr(settings_service, "_settings_find_one", lambda *_a, **_k: None)

    def _find(_collection, query, **_kwargs):
        legacy_reads.append(query)
        return [
            {
                "setting_name": primary_name,
                "value": {"provider": "ollama", "model": "gemma4:latest"},
            },
            {
                "setting_name": enabled_name,
                "value": [{"provider": "openai", "model": "gpt-5-mini"}],
            },
        ]

    monkeypatch.setattr(settings_service, "_settings_find", _find)

    configuration = settings_service._read_scoped_llm_configuration(
        canonical_setting_name=canonical_name,
        legacy_primary_setting_name=primary_name,
        legacy_enabled_setting_name=enabled_name,
        collection=collection,
    )

    assert configuration == {
        "canonical_exists": False,
        "revision": 0,
        "primary": {"provider": "ollama", "model": "gemma4:latest"},
        "enabled_llms": [
            {"provider": "ollama", "model": "gemma4:latest"},
            {"provider": "openai", "model": "gpt-5-mini"},
        ],
        "source": "legacy",
    }
    assert legacy_reads == [{"setting_name": {"$in": [primary_name, enabled_name]}}]


def test_scoped_model_configuration_completes_unambiguous_legacy_primary_host():
    primary, enabled = settings_service._normalise_primary_and_enabled_llms(
        primary={"provider": "ollama", "model": "gemma4:e4b"},
        enabled=[
            {
                "provider": "ollama",
                "model": "gemma4:e4b",
                "host": "http://127.0.0.1:11434",
            }
        ],
    )

    expected = {
        "provider": "ollama",
        "model": "gemma4:e4b",
        "host": "http://127.0.0.1:11434",
    }
    assert primary == expected
    assert enabled == [expected]


def test_first_canonical_write_migrates_legacy_pool_when_pool_is_omitted(monkeypatch):
    user_id = "#V#current_user"
    collection = object()
    monkeypatch.setattr(
        settings_service,
        "get_application_settings_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        settings_service,
        "_read_scoped_llm_configuration",
        lambda **_kwargs: {
            "canonical_exists": False,
            "revision": 0,
            "primary": {"provider": "ollama", "model": "qwen3:8b"},
            "enabled_llms": [
                {"provider": "ollama", "model": "qwen3:8b"},
                {"provider": "openai", "model": "gpt-5-mini"},
            ],
            "source": "legacy",
        },
    )
    writes: list[dict[str, Any]] = []
    monkeypatch.setattr(
        settings_service,
        "_settings_update_one",
        lambda _collection, _query, update, **_kwargs: (
            writes.append(update)
            or SimpleNamespace(
                acknowledged=True,
                upserted_id="new",
                matched_count=0,
            )
        ),
    )

    assert settings_service.set_user_llm_setting(
        user_id,
        "ollama",
        "gemma4:latest",
    )

    assert writes[0]["$set"]["value"]["enabled_llms"] == [
        {"provider": "ollama", "model": "gemma4:latest"},
        {"provider": "ollama", "model": "qwen3:8b"},
        {"provider": "openai", "model": "gpt-5-mini"},
    ]


def test_scoped_model_configuration_does_not_fall_back_when_canonical_exists(
    monkeypatch,
):
    canonical_doc = {
        "setting_name": "llm_configuration:user:#V#current_user",
        "revision": 2,
        "value": {
            "schema_version": 1,
            "primary": {"provider": "ollama", "model": "gemma4:latest"},
            "enabled_llms": [{"provider": "ollama", "model": "gemma4:latest"}],
        },
    }
    monkeypatch.setattr(
        settings_service,
        "_settings_find_one",
        lambda *_args, **_kwargs: canonical_doc,
    )
    monkeypatch.setattr(
        settings_service,
        "_settings_find",
        lambda *_args, **_kwargs: pytest.fail("legacy settings must not be read"),
    )

    configuration = settings_service._read_scoped_llm_configuration(
        canonical_setting_name=canonical_doc["setting_name"],
        legacy_primary_setting_name="active_llm:user:#V#current_user",
        legacy_enabled_setting_name="enabled_llms:user:#V#current_user",
        collection=object(),
    )

    assert configuration is not None
    assert configuration["source"] == "canonical"
    assert configuration["revision"] == 2


def test_scoped_model_configuration_retries_cas_and_preserves_latest_pool(
    monkeypatch,
):
    user_id = "#V#current_user"
    canonical_name = f"llm_configuration:user:{user_id}"
    collection = object()
    reads = iter(
        [
            {
                "canonical_exists": True,
                "revision": 3,
                "primary": {"provider": "ollama", "model": "qwen3:8b"},
                "enabled_llms": [{"provider": "ollama", "model": "qwen3:8b"}],
                "source": "canonical",
            },
            {
                "canonical_exists": True,
                "revision": 4,
                "primary": {"provider": "ollama", "model": "qwen3:8b"},
                "enabled_llms": [
                    {"provider": "ollama", "model": "qwen3:8b"},
                    {"provider": "openai", "model": "gpt-5-mini"},
                ],
                "source": "canonical",
            },
        ]
    )
    monkeypatch.setattr(
        settings_service,
        "get_application_settings_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        settings_service,
        "_read_scoped_llm_configuration",
        lambda **_kwargs: next(reads),
    )
    writes: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def _update_one(_collection, query, update, **_kwargs):
        writes.append((query, update))
        if len(writes) == 1:
            return SimpleNamespace(
                acknowledged=True,
                upserted_id=None,
                matched_count=0,
            )
        return SimpleNamespace(
            acknowledged=True,
            upserted_id=None,
            matched_count=1,
        )

    monkeypatch.setattr(settings_service, "_settings_update_one", _update_one)

    assert settings_service.set_user_llm_setting(
        user_id,
        "ollama",
        "gemma4:latest",
    )

    assert [query for query, _update in writes] == [
        {"setting_name": canonical_name, "revision": 3},
        {"setting_name": canonical_name, "revision": 4},
    ]
    assert writes[1][1]["$set"]["revision"] == 5
    assert writes[1][1]["$set"]["value"]["enabled_llms"] == [
        {"provider": "ollama", "model": "gemma4:latest"},
        {"provider": "ollama", "model": "qwen3:8b"},
        {"provider": "openai", "model": "gpt-5-mini"},
    ]


def test_scoped_same_model_update_without_host_preserves_existing_host(monkeypatch):
    user_id = "#V#current_user"
    canonical_name = f"llm_configuration:user:{user_id}"
    hosted_primary = {
        "provider": "ollama",
        "model": "gemma4:latest",
        "host": "http://127.0.0.1:11434",
    }
    prior_enabled = [
        hosted_primary,
        {"provider": "openai", "model": "gpt-5-mini"},
    ]
    collection = object()
    monkeypatch.setattr(
        settings_service,
        "get_application_settings_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        settings_service,
        "_read_scoped_llm_configuration",
        lambda **_kwargs: {
            "canonical_exists": True,
            "revision": 6,
            "primary": hosted_primary,
            "enabled_llms": prior_enabled,
            "source": "canonical",
        },
    )
    update_calls: list[tuple[dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(
        settings_service,
        "_settings_update_one",
        lambda _collection, query, update, **_kwargs: (
            update_calls.append((query, update))
            or SimpleNamespace(
                acknowledged=True,
                upserted_id=None,
                matched_count=1,
            )
        ),
    )

    assert settings_service.set_user_llm_setting(
        user_id,
        "ollama",
        "gemma4:latest",
    )

    assert len(update_calls) == 1
    query, update = update_calls[0]
    assert query == {"setting_name": canonical_name, "revision": 6}
    assert update["$set"]["value"]["primary"] == hosted_primary
    assert update["$set"]["value"]["enabled_llms"] == prior_enabled


def test_scoped_model_configuration_resolvers_project_one_complete_value(monkeypatch):
    user_id = "#V#current_user"
    canonical = {
        "canonical_exists": True,
        "revision": 9,
        "primary": {"provider": "ollama", "model": "gemma4:latest"},
        "enabled_llms": [
            {"provider": "ollama", "model": "gemma4:latest"},
            {"provider": "openai", "model": "gpt-5-mini"},
        ],
        "source": "canonical",
    }
    monkeypatch.setattr(
        settings_service,
        "_read_scoped_llm_configuration",
        lambda **_kwargs: canonical,
    )

    resolved = settings_service.resolve_llm_setting(user_concept_id=user_id)
    pool = settings_service.resolve_enabled_llm_settings(user_concept_id=user_id)

    assert resolved == {
        "provider": "ollama",
        "model": "gemma4:latest",
        "scope": "user",
        "user_concept_id": user_id,
    }
    assert pool == [
        {
            "provider": "ollama",
            "model": "gemma4:latest",
            "scope": "user",
            "user_concept_id": user_id,
        },
        {
            "provider": "openai",
            "model": "gpt-5-mini",
            "scope": "user",
            "user_concept_id": user_id,
        },
    ]


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
    rag_resolution_calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_rag_embedder_setting",
        lambda *args, **kwargs: rag_resolution_calls.append(("embedder", args, kwargs))
        or {"status": "unresolved", "effective": None},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.resolve_rag_llm_setting",
        lambda *args, **kwargs: rag_resolution_calls.append(("llm", args, kwargs))
        or {"status": "unresolved", "effective": None},
    )

    with app.test_client() as client:
        resp = client.get(
            "/api/settings/?user_concept_id=%23V%23user&organisation_concept_id=%23V%23org"
        )

    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "no-store"
    assert rag_resolution_calls == [
        ("embedder", (), {}),
        ("llm", (), {}),
    ]
    assert "workflow_capability_index" not in (resp.get_json() or {})


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
        with client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#michael_witbrock"
        resp = client.get("/api/settings/?user_concept_id=%23V%23other_user")

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
