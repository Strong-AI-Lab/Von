"""Tests for model registry-driven policy resolution."""

from __future__ import annotations

from typing import Any, Mapping, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    _WorkflowModelPolicyState,
)


class _StubGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}

    def invoke(self, _tool_name: str, _payload: Mapping[str, Any]):  # pragma: no cover
        raise AssertionError("Gateway should not be invoked in this test")


def test_policy_candidate_resolves_via_model_registry():
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _StubGateway()))

    policy_state = _WorkflowModelPolicyState(
        enabled=True,
        policy={
            "stages": {
                "planner": {
                    "primary": "#V#openaigpt5_nano20250807",
                    "fallback": [],
                }
            }
        },
        policy_id="#V#default_workflow_model_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )

    registry_snapshot = {
        "source": "vontology",
        "models": [
            {
                "model_id": "openai:gpt-5-nano-2025-08-07",
                "concept_id": "#V#openaigpt5_nano20250807",
                "registry_entry_id": "#V#openai_gpt5_nano_registry_entry",
                "provider": "OpenAI",
            }
        ],
    }

    candidates = orchestrator._stage_model_candidates(
        stage="planner",
        default_model="gpt-4",
        policy_state=policy_state,
        registry_snapshot=registry_snapshot,
    )

    assert candidates, "Expected at least one candidate"
    assert candidates[0].provider == "openai"
    assert candidates[0].model == "gpt-5-nano-2025-08-07"
    assert candidates[-1].source == "active_llm"
