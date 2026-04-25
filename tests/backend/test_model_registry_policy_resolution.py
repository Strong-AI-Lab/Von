"""Tests for model registry-driven policy resolution."""

from __future__ import annotations

from typing import Any, Mapping, cast
from unittest.mock import patch

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    _WorkflowModelPolicyState,
)
from src.backend.services.model_registry_service import (
    assess_model_stage_certification,
    build_model_stage_suitability_evidence,
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


def test_policy_candidate_prefers_active_llm_primary_before_enabled_models():
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _StubGateway()))

    policy_state = _WorkflowModelPolicyState(
        enabled=True,
        policy={
            "stages": {
                "classifier": {
                    "primary": "active_llm",
                    "fallback": [],
                }
            }
        },
        policy_id="#V#default_workflow_model_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )

    with patch(
        "src.backend.services.settings_service.resolve_enabled_llm_settings",
        return_value=[
            {"provider": "openai", "model": "gpt-4.1-mini"},
            {"provider": "ollama", "model": "granite3.3:2b"},
        ],
    ):
        candidates = orchestrator._stage_model_candidates(
            stage="classifier",
            default_model="gemma4:26b",
            policy_state=policy_state,
            registry_snapshot=None,
        )

    assert candidates, "Expected at least one candidate"
    assert candidates[0].source == "active_llm"
    assert candidates[0].raw == "active_llm"


def test_model_stage_suitability_evidence_blocks_single_case_certification():
    evidence = build_model_stage_suitability_evidence(
        model="gemma4:26b",
        stage="workflow_selector",
        workflow_id="#V#entity_information_retrieval_workflow",
        prompt_id="#V#chat_turn_classifier_prompt",
        replay_set_id="JVNAUTOSCI-1894",
        replay_case_id="represented_self_facts_vs_inferences",
        request_id="request-2090",
        verdict="passed",
        metrics={"structured_output_valid": True},
        promotion_blockers=["single_prompt_replay_evidence_only"],
    )

    assert evidence["schema_version"] == "model_stage_suitability_evidence.v1"
    assert evidence["evidence_type"] == "#V#model_stage_suitability_evidence"
    assert evidence["promotion_eligible"] is False

    decision = assess_model_stage_certification([evidence])

    assert decision["schema_version"] == "model_stage_certification_decision.v1"
    assert decision["promotion_authorised"] is False
    assert "insufficient_distinct_replay_cases" in decision["promotion_blockers"]
    assert "evidence_entry_promotion_blockers_present" in (
        decision["promotion_blockers"]
    )


def test_model_stage_certification_requires_all_evidence_to_pass():
    entries = [
        build_model_stage_suitability_evidence(
            model="gpt-5.5",
            stage="workflow_selector",
            replay_set_id="JVNAUTOSCI-1894",
            replay_case_id="case-a",
            verdict="passed",
            metrics={},
        ),
        build_model_stage_suitability_evidence(
            model="gpt-5.5",
            stage="workflow_selector",
            replay_set_id="JVNAUTOSCI-1894",
            replay_case_id="case-b",
            verdict="failed",
            metrics={},
        ),
    ]

    decision = assess_model_stage_certification(entries)

    assert decision["distinct_replay_case_count"] == 2
    assert decision["promotion_authorised"] is False
    assert decision["verdict_counts"] == {"passed": 1, "failed": 1}
    assert "non_passing_evidence_present" in decision["promotion_blockers"]
