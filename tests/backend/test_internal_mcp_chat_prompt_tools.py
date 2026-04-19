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
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

_BEHAVIOUR_PROMPT_TYPES = (
    "#V#von_chat_behaviour_prompt",
    "#V#von_chat_behavior_prompt",
    "#V#von_llm_prompt",
)


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _patch_prompt_services(monkeypatch, *, behaviour_fragments, behaviour_prompt_text):
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _namespace, **kwargs: (
            behaviour_fragments if kwargs.get("prompt_types") == _BEHAVIOUR_PROMPT_TYPES else []
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.build_user_specific_system_prompt",
        lambda _namespace, **kwargs: (
            behaviour_prompt_text
            if kwargs.get("prompt_types") == _BEHAVIOUR_PROMPT_TYPES
            else ""
        ),
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
    _patch_prompt_services(
        monkeypatch,
        behaviour_fragments=[
            {"concept_id": "#V#prompt_a", "content": "Alpha"},
            {"concept_id": "#V#prompt_b", "content": "Beta"},
        ],
        behaviour_prompt_text="Alpha\n\nBeta",
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
    _patch_prompt_services(
        monkeypatch,
        behaviour_fragments=[{"concept_id": "#V#prompt_a", "content": "Alpha"}],
        behaviour_prompt_text="Alpha",
    )

    result = _chat_get_prompt_context(
        namespace="#V#michael_witbrock", include_content=True
    )
    assert result.get("success") is True
    assert result["prompt_concepts"] == [
        {"concept_id": "#V#prompt_a", "content": "Alpha"}
    ]


def test_chat_introspect_returns_model_and_prompt_fingerprint(monkeypatch):
    class _StubOrchestrator:
        def __init__(self, *, gateway):
            self.gateway = gateway

        def _instruction_message(
            self,
            *,
            user_namespace,
            auxiliary_system_prompt,
            preferred_language,
        ):
            return (
                f"guidance::{user_namespace}::{auxiliary_system_prompt}::"
                f"{preferred_language}"
            )

    _patch_prompt_services(
        monkeypatch,
        behaviour_fragments=[{"concept_id": "#V#prompt_a", "content": "Alpha"}],
        behaviour_prompt_text="Alpha",
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.get_active_llm_setting",
        lambda: {"provider": "test", "model": "test-model"},
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_llm_setting",
        lambda **_kwargs: {"provider": "resolved", "model": "resolved-model"},
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._get_internal_mcp_chat_orchestrator_cls",
        lambda: _StubOrchestrator,
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
    assert "orchestrator_missing_tool_call_retry_cap" in result


def test_chat_introspect_redacts_sensitive_values_and_reports_presence(monkeypatch):
    _patch_prompt_services(
        monkeypatch,
        behaviour_fragments=[{"concept_id": "#V#prompt_a", "content": "Alpha"}],
        behaviour_prompt_text="Alpha",
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_llm_setting",
        lambda **_kwargs: {
            "provider": "resolved",
            "model": "resolved-model",
            "api_key": "sk-test",
            "nested": {
                "access_token": "secret-token",
                "safe_label": "keep-me",
            },
        },
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.get_setting",
        lambda name: "OPENAI_API_KEY" if name == "openai_api_key_env_var" else None,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.list_event_workflow_bindings",
        lambda limit=200: [
            {
                "event_type": "task.created",
                "workflow_id": "#V#todo_refresh_workflow",
            },
            {
                "event_type": "task.status_changed",
                "workflow_id": "#V#task_status_transition_workflow",
            },
            {
                "event_type": "message.direct_created",
                "workflow_id": "#V#direct_message_routing_workflow",
            },
        ],
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
    monkeypatch.setenv("VON_DETERMINISTIC_INTROSPECTION", "1")
    monkeypatch.setenv("VON_WORKFLOWS_TRACE_ENABLED", "0")
    monkeypatch.setenv("VON_CRITIC_ENABLE", "0")
    monkeypatch.setenv("VON_WORKFLOW_MODEL_POLICY_ENABLE", "1")
    monkeypatch.setenv("VON_MCP_ALLOW_WRITES", "0")
    monkeypatch.setenv("VON_INTERNAL_MCP_JIRA_EXECUTE_MODE", "0")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")

    result = _chat_introspect(
        namespace="#V#michael_witbrock", organisation_concept_id="#V#uoa"
    )

    assert result.get("success") is True
    assert result["introspection_version"] == "v2"
    assert result["resolved_llm"]["api_key"] is True
    assert result["resolved_llm"]["nested"]["access_token"] is True
    assert result["resolved_llm"]["nested"]["safe_label"] == "keep-me"
    assert result["configured_openai_api_key_env_var"] == "OPENAI_API_KEY"
    assert result["configured_openai_api_key_env_var_present"] is True
    assert result["sensitive_env_presence"]["OPENAI_API_KEY"] is True
    assert result["workflow_mode"]["workflow_selector_enabled"] is True
    assert result["workflow_mode"]["deterministic_introspection_enabled"] is True
    assert result["workflow_mode"]["workflow_model_policy_enabled"] is True
    assert result["workflow_mode"]["durable_workflows_enabled"] is True
    assert result["workflow_mode"]["event_workflow_integration_enabled"] is True
    assert result["workflow_mode"]["runtime_mode"] == "workflow_routed_tool_calling"
    assert result["event_workflow_bindings"]["task.created"] == "#V#todo_refresh_workflow"
    assert (
        result["event_workflow_bindings"]["task.status_changed"]
        == "#V#task_status_transition_workflow"
    )
    assert (
        result["event_workflow_bindings"]["message.direct_created"]
        == "#V#direct_message_routing_workflow"
    )


def test_chat_introspect_gateway_invoke_success_path(monkeypatch):
    _patch_prompt_services(
        monkeypatch,
        behaviour_fragments=[{"concept_id": "#V#prompt_a", "content": "Alpha"}],
        behaviour_prompt_text="Alpha",
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_llm_setting",
        lambda **_kwargs: {
            "provider": "resolved",
            "model": "resolved-model",
            "api_token": "token-value",
        },
    )

    gateway = _build_gateway()
    result = gateway.invoke(
        "chat_introspect",
        {"namespace": "#V#michael_witbrock", "organisation_concept_id": "#V#uoa"},
    )
    payload = result.payload
    assert payload.get("success") is True
    assert payload.get("introspection_version") == "v2"
    assert payload.get("active_model_name") == "test-model"
    assert "behaviour_prompt_concept_ids" in payload
    assert "narration_prompt_concept_ids" in payload
    assert payload["resolved_llm"]["api_token"] is True


def test_chat_introspect_gateway_invoke_handler_error_path():
    gateway = _build_gateway()
    result = gateway.invoke("chat_introspect", {"namespace": "   "})
    payload = result.payload
    assert payload.get("success") is False
    assert payload.get("error_code") == "missing_parameter"


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
