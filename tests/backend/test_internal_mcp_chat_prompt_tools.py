"""Smoke tests for chat prompt introspection tools in the internal MCP catalogue.

These tests avoid hitting the real database and only verify registration and
basic handler behaviour.
"""

from src.backend.integrations.internal_mcp.catalogue import (
    build_default_catalogue,
    _chat_get_prompt_context,
    _chat_introspect,
    _settings_get_public,
)


def test_chat_prompt_tool_registered_in_catalogue():
    catalogue = build_default_catalogue()
    names = set(catalogue.list_methods())
    assert "chat_get_prompt_context" in names
    assert "chat_introspect" in names
    assert "settings_get_public" in names


def test_chat_get_prompt_context_requires_namespace():
    result = _chat_get_prompt_context(namespace=None)
    assert result.get("success") is False
    assert "namespace" in (result.get("error") or "")


def test_chat_introspect_requires_namespace():
    result = _chat_introspect(namespace=None)
    assert result.get("success") is False
    assert "namespace" in (result.get("error") or "")


def test_chat_get_prompt_context_returns_prompt_metadata(monkeypatch):
    # Patch service imports used inside handler
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _namespace: [
            {"concept_id": "#V#prompt_a", "content": "Alpha"},
            {"concept_id": "#V#prompt_b", "content": "Beta"},
        ],
    )
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.build_user_specific_system_prompt",
        lambda _namespace: "Alpha\n\nBeta",
    )

    result = _chat_get_prompt_context(
        namespace="#V#michael_witbrock", include_content=False
    )
    assert result.get("success") is True
    assert result["namespace"] == "#V#michael_witbrock"
    assert result["prompt_concept_ids"] == ["#V#prompt_a", "#V#prompt_b"]
    assert result["prompt_count"] == 2
    assert result["prompt_text"] == "Alpha\n\nBeta"

    # No content when include_content=False
    assert result["prompt_concepts"] == [
        {"concept_id": "#V#prompt_a"},
        {"concept_id": "#V#prompt_b"},
    ]


def test_chat_get_prompt_context_includes_content_when_requested(monkeypatch):
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _namespace: [
            {"concept_id": "#V#prompt_a", "content": "Alpha"},
        ],
    )
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.build_user_specific_system_prompt",
        lambda _namespace: "Alpha",
    )

    result = _chat_get_prompt_context(
        namespace="#V#michael_witbrock", include_content=True
    )
    assert result.get("success") is True
    assert result["prompt_concepts"] == [
        {"concept_id": "#V#prompt_a", "content": "Alpha"}
    ]


def test_chat_introspect_returns_model_and_prompt_fingerprint(monkeypatch):
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _namespace: [{"concept_id": "#V#prompt_a", "content": "Alpha"}],
    )
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.build_user_specific_system_prompt",
        lambda _namespace: "Alpha",
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_name",
        lambda: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.get_active_llm_setting",
        lambda: {"provider": "test", "model": "test-model"},
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_llm_setting",
        lambda **_kwargs: {"provider": "resolved", "model": "resolved-model"},
    )

    result = _chat_introspect(
        namespace="#V#michael_witbrock", organisation_concept_id="#V#uoa"
    )
    assert result.get("success") is True
    assert result["active_model_name"] == "test-model"
    assert result["organisation_concept_id"] == "#V#uoa"
    assert result["resolved_llm"] == {"provider": "resolved", "model": "resolved-model"}
    assert result["prompt_concept_ids"] == ["#V#prompt_a"]
    assert result["tool_guidance_hash"], "expected a tool guidance hash"
    assert "gateway_enabled" in result
    assert "orchestrator_max_tool_invocations" in result


def test_settings_get_public_returns_settings(monkeypatch):
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.get_all_settings_data",
        lambda: {"active_llm": {"provider": "x", "model": "y"}},
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_llm_setting",
        lambda **_kwargs: {"provider": "x", "model": "y"},
    )

    result = _settings_get_public(user_concept_id="#V#michael_witbrock")
    assert result.get("success") is True
    assert "settings" in result
    assert result["settings"]["active_llm"]["model"] == "y"
