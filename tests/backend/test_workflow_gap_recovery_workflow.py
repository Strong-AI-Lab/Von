"""Tests for workflow-gap recovery durable workflows."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult
from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable.workflow_gap_recovery_workflow import (
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    WORKFLOW_GAP_DECIDE_TEST_ACTION_ID,
    WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID,
    WORKFLOW_GAP_FINALISE_RECOVERY_ACTION_ID,
    WORKFLOW_GAP_RUN_CANDIDATE_TEST_ACTION_ID,
    WORKFLOW_GAP_TEST_WORKFLOW_ID,
    build_workflow_discovery_gap_recovery_workflow_test_definition,
    build_workflow_gap_test_definition,
    register_workflow_gap_recovery_actions,
)


def test_build_workflow_gap_recovery_definition_shape() -> None:
    workflow = build_workflow_discovery_gap_recovery_workflow_test_definition()

    assert workflow.workflow_id == WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID
    assert workflow.initial_state == "collect_context"
    assert set(workflow.states.keys()) == {
        "collect_context",
        "analyse_gap",
        "prepare_candidate",
        "create_candidate",
        "decide_test",
        "test_candidate",
        "complete",
        "failed",
    }
    assert workflow.termination_states == ("complete", "failed")

    test_workflow = build_workflow_gap_test_definition()
    assert test_workflow.workflow_id == WORKFLOW_GAP_TEST_WORKFLOW_ID
    assert test_workflow.initial_state == "run_test"
    assert set(test_workflow.states.keys()) == {"run_test", "complete", "failed"}


def test_register_workflow_gap_recovery_actions_registers_expected_ids() -> None:
    registry = ActionRegistry()
    register_workflow_gap_recovery_actions(registry)

    assert {
        "workflow_gap.collect_context",
        "workflow_gap.analyse_recovery",
        "workflow_gap.prepare_candidate_spec",
        WORKFLOW_GAP_DECIDE_TEST_ACTION_ID,
        WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID,
        WORKFLOW_GAP_RUN_CANDIDATE_TEST_ACTION_ID,
        WORKFLOW_GAP_FINALISE_RECOVERY_ACTION_ID,
    }.issubset(set(registry.all_action_ids()))


def test_finalise_recovery_asks_user_when_candidate_needs_confirmation(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.workflow_gap_recovery_workflow as mod

    monkeypatch.setattr(
        mod,
        "_link_candidate_prompt_to_workflow",
        lambda **_kwargs: None,
    )

    registry = ActionRegistry()
    register_workflow_gap_recovery_actions(registry)

    result = registry.execute(
        WORKFLOW_GAP_FINALISE_RECOVERY_ACTION_ID,
        inputs={},
        context={
            "workflow_gap_analysis_result": {
                "decision": "ask_user",
                "gap_summary": "A reusable workflow for this request is missing.",
                "user_confirmation_prompt": "Should I keep and use this workflow?",
            },
            "workflow_gap_base_response_text": "Fallback reply.",
            "created_candidate_workflow_id": "#V#candidate_gap_workflow",
            "candidate_workflow_name": "Candidate gap workflow",
            "candidate_prompt_concept_id": "#V#candidate_gap_prompt",
        },
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert (
        result.outputs["workflow_gap_recovery_outcome"]
        == "candidate_created_awaiting_confirmation"
    )
    assert "Candidate gap workflow (#V#candidate_gap_workflow)" in result.outputs[
        "workflow_gap_final_response_text"
    ]
    assert "Should I keep and use this workflow?" in result.outputs[
        "workflow_gap_final_response_text"
    ]


def test_execute_candidate_disables_recursive_gap_recovery(monkeypatch) -> None:
    import src.backend.workflows.durable.workflow_gap_recovery_workflow as mod

    captured: dict[str, object] = {}

    class _FakePromptService:
        def __init__(self, *, default_max_chars: int = 0) -> None:
            self.default_max_chars = default_max_chars

        def render_prompt(self, *_args, **_kwargs):
            return SimpleNamespace(text="Candidate prompt")

    class _FakeOrchestrator:
        def __init__(
            self,
            *,
            gateway,
            max_tool_invocations,
            default_gmail_profile=None,
        ) -> None:
            captured["max_tool_invocations"] = max_tool_invocations
            captured["gateway"] = gateway
            captured["default_gmail_profile"] = default_gmail_profile

        def run(self, **kwargs):
            captured["run_kwargs"] = dict(kwargs)
            return OrchestratorResult(
                response_text="Retried response.",
                extra_messages=({"role": "tool", "content": "tool output"},),
                tool_invocations=({"tool": "search_concepts"},),
                aux_llm_calls=(),
                llm_calls=(),
            )

    monkeypatch.setattr(mod, "PromptTemplateService", _FakePromptService)
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _FakeOrchestrator,
    )

    registry = ActionRegistry()
    register_workflow_gap_recovery_actions(registry)
    result = registry.execute(
        WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID,
        inputs={
            "prompt_concept_id": "#V#candidate_gap_prompt",
            "workflow_gap_dry_run": True,
            "prompt": "Please recover this workflow gap.",
        },
        context={
            "context": [{"role": "user", "content": "Please recover this workflow gap."}],
            "workflow_gap_base_response_text": "Fallback reply.",
        },
        env=WorkflowEnvironment(
            llm_client=object(),
            gateway=SimpleNamespace(enabled=True),
            model="gpt-5.2-chat-latest",
            user_namespace="#V#user",
        ),
    )

    assert result.status == "success"
    assert result.outputs["response_text"] == "Retried response."
    assert captured["max_tool_invocations"] == 0
    run_kwargs = cast(dict[str, Any], captured["run_kwargs"])
    assert run_kwargs["workflow_gap_recovery_enabled"] is False
    assert run_kwargs["prompt"] == "Please recover this workflow gap."


def test_execute_candidate_uses_canonical_cap_default_when_env_cap_missing(
    monkeypatch,
) -> None:
    import src.backend.workflows.durable.workflow_gap_recovery_workflow as mod

    captured: dict[str, object] = {}

    class _FakePromptService:
        def __init__(self, *, default_max_chars: int = 0) -> None:
            self.default_max_chars = default_max_chars

        def render_prompt(self, *_args, **_kwargs):
            return SimpleNamespace(text="Candidate prompt")

    class _FakeOrchestrator:
        def __init__(
            self,
            *,
            gateway,
            max_tool_invocations,
            default_gmail_profile=None,
        ) -> None:
            captured["max_tool_invocations"] = max_tool_invocations

        def run(self, **kwargs):
            return OrchestratorResult(
                response_text="Retried response.",
                extra_messages=(),
                tool_invocations=(),
                aux_llm_calls=(),
                llm_calls=(),
            )

    monkeypatch.setattr(mod, "PromptTemplateService", _FakePromptService)
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _FakeOrchestrator,
    )

    registry = ActionRegistry()
    register_workflow_gap_recovery_actions(registry)
    result = registry.execute(
        WORKFLOW_GAP_EXECUTE_CANDIDATE_ACTION_ID,
        inputs={
            "prompt_concept_id": "#V#candidate_gap_prompt",
            "workflow_gap_dry_run": False,
            "prompt": "Please recover this workflow gap.",
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=object(),
            gateway=SimpleNamespace(enabled=True),
            model="gpt-5.2-chat-latest",
            max_tool_invocations=None,
        ),
    )

    assert result.status == "success"
    assert (
        captured["max_tool_invocations"]
        == mod.INTERNAL_MCP_MAX_TOOL_INVOCATIONS_DEFAULT
    )


def test_workflow_gap_recovery_workflows_publish_authoritatively(monkeypatch) -> None:
    from src.backend.db.mongo_client import get_db
    from src.backend.services.workflow_discovery_service import (
        invalidate_workflow_discovery_executability_caches,
    )
    from src.backend.workflows import (
        workflow_concept_authority_service as authority_service,
    )
    from workflow_test_support import (
        bootstrap_authoritative_reasoning_recovery_workflows,
    )

    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    authority_service.clear_workflow_type_resolution_cache()

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass

    invalidate_workflow_discovery_executability_caches()
    report = bootstrap_authoritative_reasoning_recovery_workflows()
    graph_publication = report.get("graph_publication") or {}
    published_ids = set(graph_publication.get("published_workflow_ids") or [])
    errors_by_workflow_id = graph_publication.get("errors_by_workflow_id") or {}

    assert WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID in published_ids
    assert WORKFLOW_GAP_TEST_WORKFLOW_ID in published_ids
    assert WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID not in errors_by_workflow_id
    assert WORKFLOW_GAP_TEST_WORKFLOW_ID not in errors_by_workflow_id
