from __future__ import annotations

from typing import Any, Mapping

from src.backend.integrations.internal_mcp.orchestrator import _WorkflowModelPolicyState
from src.backend.services.workflow_model_selection_workflow_vontology_service import (
    bootstrap_canonical_workflow_model_selection_workflow,
)
from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.definitions import WORKFLOW_MODEL_SELECTION_WORKFLOW_ID
from src.backend.workflows.durable.model_selection_workflow import (
    WORKFLOW_MODEL_SELECTION_ACTION_ID,
    WORKFLOW_MODEL_SELECTION_SCHEMA_VERSION,
    register_model_selection_actions,
)


def test_model_selection_action_returns_enabled_pool_and_workflow_policy_choice(
    monkeypatch,
) -> None:
    policy_state = _WorkflowModelPolicyState(
        enabled=True,
        policy={
            "stages": {"planner": {"primary": "active_llm", "fallback": []}},
            "workflows": {
                WORKFLOW_MODEL_SELECTION_WORKFLOW_ID: {
                    "stages": {
                        "planner": {
                            "primary": "openai:gpt-5.2-chat-latest",
                            "fallback": ["active_llm"],
                        }
                    }
                }
            },
        },
        policy_id="#V#default_workflow_model_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator."
        "InternalMCPChatOrchestrator._load_workflow_model_policy",
        lambda self, _preferred_language: (
            policy_state,
            {"policy_source": "test", "loaded": True},
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.model_registry_service.get_model_registry_snapshot",
        lambda: {"source": "test", "models": []},
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_enabled_llm_settings",
        lambda **_kwargs: [
            {"provider": "openai", "model": "gpt-5.2-chat-latest", "scope": "user"},
            {"provider": "openai", "model": "gpt-transcribe", "scope": "user"},
            {"provider": "ollama", "model": "granite3.3:2b", "scope": "user"},
        ],
    )

    registry = ActionRegistry()
    register_model_selection_actions(registry)
    context: dict[str, Any] = {
        "stage": "planner",
        "workflow_id": WORKFLOW_MODEL_SELECTION_WORKFLOW_ID,
        "user_concept_id": "#V#user",
    }

    result = registry.execute(
        WORKFLOW_MODEL_SELECTION_ACTION_ID,
        inputs={},
        context=context,
        env=WorkflowEnvironment(llm_client=None, model="gemma4:26b"),
        workflow_id=WORKFLOW_MODEL_SELECTION_WORKFLOW_ID,
        workflow_state_id="select_model",
    )

    assert result.ok
    assert result.outputs["selected_model"] == "gpt-5.2-chat-latest"
    assert result.outputs["selected_model_provider"] == "openai"
    assert result.outputs["enabled_model_pool"] == [
        {
            "provider": "openai",
            "model": "gpt-5.2-chat-latest",
            "host": None,
            "scope": "user",
            "source": "enabled_settings",
        },
        {
            "provider": "ollama",
            "model": "granite3.3:2b",
            "host": None,
            "scope": "user",
            "source": "enabled_settings",
        },
    ]
    model_selection = result.outputs["model_selection"]
    assert model_selection["schema_version"] == WORKFLOW_MODEL_SELECTION_SCHEMA_VERSION
    assert model_selection["workflow_id"] == WORKFLOW_MODEL_SELECTION_WORKFLOW_ID
    assert model_selection["selection_metadata"]["policy_scope"] == "workflow"


def test_model_selection_workflow_bootstrap_publishes_action_step(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def _publish_workflow_definition_from_definition(
        *,
        definition: Any,
        create_missing: bool,
        purpose: str,
    ) -> Mapping[str, Any]:
        captured["definition"] = definition
        captured["create_missing"] = create_missing
        captured["purpose"] = purpose
        return {"errors_by_workflow_id": {}, "validation_failures_by_workflow_id": {}}

    lifecycle_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.backend.services.workflow_model_selection_workflow_vontology_service."
        "authority_service.publish_workflow_definition_from_definition",
        _publish_workflow_definition_from_definition,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_model_selection_workflow_vontology_service."
        "authority_service.upsert_workflow_publication_lifecycle",
        lambda **kwargs: lifecycle_calls.append(dict(kwargs)),
    )

    report = bootstrap_canonical_workflow_model_selection_workflow()

    assert report["success"] is True
    definition = captured["definition"]
    assert definition.workflow_id == WORKFLOW_MODEL_SELECTION_WORKFLOW_ID
    assert definition.initial_state == "select_model"
    select_state = definition.states["select_model"]
    assert select_state.actions[0].target_id == WORKFLOW_MODEL_SELECTION_ACTION_ID
    assert "enabled_model_pool" in select_state.metadata["writes_context_keys"]
    assert lifecycle_calls == [
        {
            "workflow_id": WORKFLOW_MODEL_SELECTION_WORKFLOW_ID,
            "phase": "published",
            "published": True,
            "validation_passed": True,
            "postconditions_verified": True,
        }
    ]