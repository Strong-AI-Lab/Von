"""Smoke tests for chat prompt introspection tools in the internal MCP catalogue.

These tests avoid hitting the real database and only verify registration and
basic handler behaviour.
"""

from src.backend.integrations.internal_mcp.catalogue import (
    _chat_get_applied_prompt_context,
    _chat_get_prompt_context,
    _chat_introspect,
    _settings_get_public,
    build_default_catalogue,
)
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.services.adaptive_turn_service import (
    _model_visible_input_schema,
    _trusted_tool_payload,
    ordinary_turn_capability_delegation,
)
from src.backend.services.chat_auxiliary_prompt_service import (
    build_applied_prompt_snapshot,
    serialise_applied_prompt_snapshot,
)

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
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_enabled_llm_settings",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.get_setting",
        lambda _name: None,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.list_event_workflow_bindings",
        lambda limit=200: [],
    )
    monkeypatch.setattr(
        "src.backend.services.coding_agent_mcp_access_profile_service.build_coding_agent_mcp_access_profile",
        lambda: {"success": True, "profile_id": "test"},
    )


def test_chat_prompt_tool_registered_in_catalogue():
    catalogue = build_default_catalogue()
    names = set(catalogue.list_methods())
    assert "chat_get_prompt_context" in names
    assert "chat_get_applied_prompt_context" in names
    assert "chat_introspect" in names
    assert "settings_get_public" in names


def _applied_prompt_snapshot_json(user_concept_id="#V#michael_witbrock"):
    snapshot = build_applied_prompt_snapshot(
        user_concept_id=user_concept_id,
        namespace=f"{user_concept_id}@university_of_auckland_strong_ai_lab",
        organisation_concept_id="#V#university_of_auckland_strong_ai_lab",
        turn_id="turn-123",
        behaviour_fragments=[
            {"concept_id": "#V#behaviour_prompt", "content": "Be precise."},
        ],
        narration_fragments=[
            {"concept_id": "#V#narration_prompt", "content": "Speak plainly."},
        ],
        screen_fragments=[
            {"concept_id": "#V#screen_prompt", "content": "Use Markdown."},
        ],
    )
    return serialise_applied_prompt_snapshot(snapshot)


def test_chat_get_applied_prompt_context_returns_exact_turn_snapshot():
    result = _chat_get_applied_prompt_context(
        applied_prompt_snapshot_json=_applied_prompt_snapshot_json(),
        include_content=True,
        namespace="#V#attempted_other_user",
    )

    assert result["success"] is True
    assert result["source"] == "server_bound_turn_snapshot"
    assert result["turn_id"] == "turn-123"
    assert result["actor"]["user_concept_id"] == "#V#michael_witbrock"
    assert result["prompt_classes"] == ["behaviour", "narration", "screen"]
    assert result["prompt_concept_ids"] == [
        "#V#behaviour_prompt",
        "#V#narration_prompt",
        "#V#screen_prompt",
    ]
    assert [item["content"] for item in result["prompts"]] == [
        "Be precise.",
        "Speak plainly.",
        "Use Markdown.",
    ]
    assert all(len(item["content_sha256"]) == 64 for item in result["prompts"])


def test_chat_get_applied_prompt_context_requires_server_bound_snapshot():
    result = _chat_get_applied_prompt_context(
        applied_prompt_snapshot_json=None,
        include_content=True,
    )
    assert result["success"] is False
    assert result["error_code"] == "trusted_turn_snapshot_required"


def test_chat_get_applied_prompt_context_rejects_modified_snapshot():
    snapshot_json = _applied_prompt_snapshot_json().replace(
        "Be precise.", "Read another user's prompt."
    )
    result = _chat_get_applied_prompt_context(
        applied_prompt_snapshot_json=snapshot_json,
        include_content=True,
    )
    assert result["success"] is False
    assert result["error_code"] == "invalid_trusted_turn_snapshot"


def test_applied_prompt_capability_is_delegated_only_with_trusted_snapshot():
    gateway = _build_gateway()
    definition = gateway.get_method_definition("chat_get_applied_prompt_context")
    assert definition is not None
    assert definition.ordinary_turn_excluded_reason is None
    assert definition.ordinary_turn_trusted_argument_bindings == {
        "applied_prompt_snapshot_json": "applied_prompt_snapshot"
    }

    without_snapshot = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#michael_witbrock",
        trusted_argument_values=None,
    )
    assert "chat_get_applied_prompt_context" not in without_snapshot

    trusted_snapshot = _applied_prompt_snapshot_json()
    with_snapshot = ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#michael_witbrock",
        trusted_argument_values={"applied_prompt_snapshot": trusted_snapshot},
    )
    assert "chat_get_applied_prompt_context" in with_snapshot

    visible_schema = _model_visible_input_schema(
        definition,
        {"applied_prompt_snapshot": trusted_snapshot},
    )
    assert "applied_prompt_snapshot_json" not in visible_schema["properties"]
    assert "applied_prompt_snapshot_json" not in visible_schema.get("required", [])

    payload = _trusted_tool_payload(
        gateway=gateway,
        tool_name="chat_get_applied_prompt_context",
        model_payload={
            "applied_prompt_snapshot_json": _applied_prompt_snapshot_json(
                "#V#attempted_other_user"
            ),
            "include_content": False,
        },
        trusted_argument_values={"applied_prompt_snapshot": trusted_snapshot},
    )
    assert payload["applied_prompt_snapshot_json"] == trusted_snapshot


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
    assert set(result["resolved_templates"]) == {
        "behaviour_prompt",
        "narration_prompt",
    }


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

    result = _chat_introspect(
        namespace="#V#michael_witbrock",
        organisation_concept_id="#V#uoa",
        include_tool_guidance_preview=True,
    )
    assert result.get("success") is True
    assert result["active_model_name"] == "test-model"
    assert result["organisation_concept_id"] == "#V#uoa"
    assert result["resolved_llm"] == {"provider": "resolved", "model": "resolved-model"}
    assert result["prompt_concept_ids"] == ["#V#prompt_a"]
    assert result["tool_guidance_hash"], "expected a tool guidance hash"
    assert "direct adaptive turn path" in result["tool_guidance_preview"]
    assert "turn_capabilities" in result["tool_guidance_preview"]
    assert "gateway_enabled" in result
    assert result["orchestrator_max_tool_invocations"] is None
    assert result["orchestrator_missing_tool_call_retry_cap"] is None
    assert result["workflow_mode"]["ordinary_turn_path"] == "direct_adaptive_turn"
    assert (
        result["workflow_mode"]["ordinary_turn_capability_mode"]
        == "bounded_capabilities"
    )
    assert (
        result["workflow_mode"]["automatic_workflow_selector_enabled"] is False
    )


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
    monkeypatch.setenv("OPENAI_API_BACKUP_KEY", "sk-backup")
    monkeypatch.setenv("VON_WORKFLOWS_TRACE_ENABLED", "0")
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
    assert result["sensitive_env_presence"]["OPENAI_API_BACKUP_KEY"] is True
    assert "sk-backup" not in str(result)
    assert result["workflow_mode"]["ordinary_turn_path"] == "direct_adaptive_turn"
    assert result["workflow_mode"]["automatic_workflow_selector_enabled"] is False
    assert result["workflow_mode"]["legacy_orchestrator_status"] == "retired"
    assert result["workflow_mode"]["explicit_workflow_model_policy_enabled"] is True
    assert result["workflow_mode"]["explicit_workflows_enabled"] is True
    assert result["workflow_mode"]["event_workflow_integration_enabled"] is True
    assert result["workflow_mode"]["runtime_mode"] == "direct_adaptive_capabilities"
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
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_enabled_llm_settings",
        lambda **_kwargs: [],
    )

    result = _settings_get_public(user_concept_id="#V#michael_witbrock")
    assert result.get("success") is True
    assert "settings" in result
    assert result["settings"]["active_llm"]["model"] == "y"
