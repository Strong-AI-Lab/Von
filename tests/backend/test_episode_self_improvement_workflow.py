from __future__ import annotations

import json
from typing import Any

import pytest

from src.backend.db.mongo_client import get_db
from src.backend.services.episode_evaluation_workflow_contracts import (
    EPISODE_SELF_IMPROVEMENT_PROMOTION_WORKFLOW_ID,
    EPISODE_SELF_IMPROVEMENT_PROPOSAL_WORKFLOW_ID,
)
from src.backend.services.episode_evaluation_workflow_vontology_service import (
    bootstrap_canonical_episode_evaluation_workflow,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.episode_self_improvement_workflow import (
    register_episode_self_improvement_actions,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology


class _QueuedLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    def generate(
        self,
        prompt: str,
        context: Any = None,
        model: str | None = None,
        llm_params: dict[str, Any] | None = None,
    ) -> str:
        _ = (prompt, context, model, llm_params)
        if not self._responses:
            raise AssertionError("queued_llm_exhausted")
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    db = get_db()
    if db is not None:
        for collection_name in (
            "concepts",
            "text_relations",
            "text_values",
            "workflow_event_bindings",
            "workflow_instances",
        ):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass

    yield
    authority_service.clear_workflow_type_resolution_cache()


def test_episode_self_improvement_proposal_workflow_executes_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.workflows.durable.episode_self_improvement_workflow as mod

    bootstrap_canonical_episode_evaluation_workflow()
    definition = load_workflow_definition_from_vontology(
        EPISODE_SELF_IMPROVEMENT_PROPOSAL_WORKFLOW_ID
    )
    assert definition is not None

    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        mod,
        "build_workflow_improvement_context",
        lambda **kwargs: {
            "success": True,
            "episode_critique_memory_id": kwargs["episode_critique_memory_id"],
            "target_workflow_id": kwargs["target_workflow_id"],
            "target_workflow_definition_identity": {
                "workflow_id": kwargs["target_workflow_id"],
                "definition_hash": "base-hash-1",
            },
            "base_definition_hash": "base-hash-1",
            "existing_workflow_spec": {
                "workflow_id": kwargs["target_workflow_id"],
                "steps": [{"state_id": "complete", "terminal": True}],
            },
            "episode_self_improvement_suggestion": {
                "suggestion_id": kwargs["suggestion_id"],
                "target_workflow_id": kwargs["target_workflow_id"],
                "target_surface": "workflow",
                "category": "workflow_change",
                "title": "Tighten route selection",
                "rationale": "Critique evidence showed the wrong workflow was used.",
                "suggested_change": "Restrict the eligible workflow branch.",
            },
            "episode_self_improvement_benchmark_summary": {
                "benchmark_fingerprint": "bench-1",
                "benchmark_signal_summary": {"fail": 1},
            },
            "proposal_context": {
                "schema_version": "episode_self_improvement_proposal_context.v1",
                "episode_critique_memory_id": kwargs["episode_critique_memory_id"],
            },
            "workflow_authoring_prompt_contract": {
                "required_keys": ["workflow_id", "steps"]
            },
            "workflow_authoring_prompt_health": {"healthy": True},
        },
    )

    def _submit(**kwargs: Any) -> dict[str, Any]:
        captured["submit"] = kwargs
        return {
            "success": True,
            "proposal": {
                "proposal_id": "proposal-1",
                "status": "pending_review",
            },
            "candidate_validation": {"valid": True},
            "promotion_launch_inputs": {
                "episode_critique_memory_id": kwargs["episode_critique_memory_id"],
                "target_workflow_id": kwargs["target_workflow_id"],
                "proposal_id": "proposal-1",
                "suggestion_id": kwargs["suggestion"]["suggestion_id"],
                "namespace": kwargs["namespace"],
                "user_id": kwargs["user_id"],
                "org_id": kwargs["org_id"],
                "episode_evaluation_depth": kwargs["current_depth"] + 1,
            },
            "promotion_launch_source_event_id": "proposal-1",
            "promotion_launch_event_idempotency_key": "promotion-key-1",
        }

    monkeypatch.setattr(mod, "submit_workflow_improvement_proposal", _submit)

    registry = ActionRegistry()
    register_episode_self_improvement_actions(registry)

    def _launch_promotion(request) -> WorkflowActionResult:
        captured["promotion_launch"] = dict(request.inputs)
        return WorkflowActionResult(
            status="success",
            outputs={
                "instance_id": "#V#wf_instance_promotion_1",
                "status": "created",
            },
        )

    registry.register(
        ActionSpec(
            action_id="workflow_create_instance",
            handler=_launch_promotion,
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_QueuedLLM(
                [
                    json.dumps(
                        {
                            "target_workflow_id": "#V#alpha_workflow",
                            "repair_summary": "Restrict the discovery path.",
                            "repaired_workflow_spec": {
                                "workflow_id": "#V#alpha_workflow",
                                "steps": [{"state_id": "complete", "terminal": True}],
                            },
                        }
                    )
                ]
            ),
            user_namespace="#V#user@org",
        ),
        data={
            "episode_critique_memory_id": "#V#episode_critique_memory_1",
            "suggestion_id": "workflow_change_alpha",
            "target_workflow_id": "#V#alpha_workflow",
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "episode_evaluation_depth": 0,
        },
    )

    assert result.completed is True
    assert result.error is None
    assert result.data["proposal_id"] == "proposal-1"
    assert result.data["proposal_status"] == "pending_review"
    assert result.data["promotion_workflow_instance_id"] == (
        "#V#wf_instance_promotion_1"
    )
    assert captured["submit"]["target_workflow_id"] == "#V#alpha_workflow"
    assert captured["submit"]["candidate_workflow_spec"]["workflow_id"] == (
        "#V#alpha_workflow"
    )
    assert captured["promotion_launch"]["workflow_id"] == (
        EPISODE_SELF_IMPROVEMENT_PROMOTION_WORKFLOW_ID
    )
    assert captured["promotion_launch"]["inputs"]["proposal_id"] == "proposal-1"


def test_episode_self_improvement_proposal_workflow_accepts_user_only_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.workflows.durable.episode_self_improvement_workflow as mod

    bootstrap_canonical_episode_evaluation_workflow()
    definition = load_workflow_definition_from_vontology(
        EPISODE_SELF_IMPROVEMENT_PROPOSAL_WORKFLOW_ID
    )
    assert definition is not None

    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        mod,
        "build_workflow_improvement_context",
        lambda **kwargs: {
            "success": True,
            "episode_critique_memory_id": kwargs["episode_critique_memory_id"],
            "target_workflow_id": kwargs["target_workflow_id"],
            "target_workflow_definition_identity": {
                "workflow_id": kwargs["target_workflow_id"],
                "definition_hash": "base-hash-user-only",
            },
            "base_definition_hash": "base-hash-user-only",
            "existing_workflow_spec": {
                "workflow_id": kwargs["target_workflow_id"],
                "steps": [{"state_id": "complete", "terminal": True}],
            },
            "episode_self_improvement_suggestion": {
                "suggestion_id": kwargs["suggestion_id"],
                "target_workflow_id": kwargs["target_workflow_id"],
                "target_surface": "workflow",
                "category": "workflow_change",
                "title": "Tighten route selection",
                "rationale": "Critique evidence showed the wrong workflow was used.",
                "suggested_change": "Restrict the eligible workflow branch.",
            },
            "episode_self_improvement_benchmark_summary": {
                "benchmark_fingerprint": "bench-user-only",
                "benchmark_signal_summary": {"fail": 1},
            },
            "proposal_context": {
                "schema_version": "episode_self_improvement_proposal_context.v1",
                "episode_critique_memory_id": kwargs["episode_critique_memory_id"],
            },
            "workflow_authoring_prompt_contract": {
                "required_keys": ["workflow_id", "steps"]
            },
            "workflow_authoring_prompt_health": {"healthy": True},
        },
    )

    def _submit(**kwargs: Any) -> dict[str, Any]:
        captured["submit"] = kwargs
        return {
            "success": True,
            "proposal": {
                "proposal_id": "proposal-user-only",
                "status": "pending_review",
            },
            "candidate_validation": {"valid": True},
            "promotion_launch_inputs": {
                "episode_critique_memory_id": kwargs["episode_critique_memory_id"],
                "target_workflow_id": kwargs["target_workflow_id"],
                "proposal_id": "proposal-user-only",
                "suggestion_id": kwargs["suggestion"]["suggestion_id"],
                "namespace": kwargs["namespace"],
                "user_id": kwargs["user_id"],
                "org_id": kwargs["org_id"],
                "episode_evaluation_depth": kwargs["current_depth"] + 1,
            },
            "promotion_launch_source_event_id": "proposal-user-only",
            "promotion_launch_event_idempotency_key": "promotion-key-user-only",
        }

    monkeypatch.setattr(mod, "submit_workflow_improvement_proposal", _submit)

    registry = ActionRegistry()
    register_episode_self_improvement_actions(registry)

    def _launch_promotion(request) -> WorkflowActionResult:
        captured["promotion_launch"] = dict(request.inputs)
        return WorkflowActionResult(
            status="success",
            outputs={
                "instance_id": "#V#wf_instance_promotion_user_only",
                "status": "created",
            },
        )

    registry.register(
        ActionSpec(
            action_id="workflow_create_instance",
            handler=_launch_promotion,
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_QueuedLLM(
                [
                    json.dumps(
                        {
                            "target_workflow_id": "#V#alpha_workflow",
                            "repair_summary": "Restrict the discovery path.",
                            "repaired_workflow_spec": {
                                "workflow_id": "#V#alpha_workflow",
                                "steps": [{"state_id": "complete", "terminal": True}],
                            },
                        }
                    )
                ]
            ),
            user_namespace="#V#user",
        ),
        data={
            "episode_critique_memory_id": "#V#episode_critique_memory_user_only",
            "suggestion_id": "workflow_change_alpha",
            "target_workflow_id": "#V#alpha_workflow",
            "namespace": "#V#user",
            "user_id": "#V#user",
            "episode_evaluation_depth": 0,
        },
    )

    assert result.completed is True
    assert result.error is None
    assert result.data["proposal_id"] == "proposal-user-only"
    assert captured["submit"]["namespace"] == "#V#user"
    assert captured["submit"]["user_id"] == "#V#user"
    assert captured["submit"]["org_id"] is None
    assert captured["promotion_launch"]["inputs"]["org_id"] is None


def test_episode_self_improvement_promotion_workflow_executes_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.workflows.durable.episode_self_improvement_workflow as mod

    bootstrap_canonical_episode_evaluation_workflow()
    definition = load_workflow_definition_from_vontology(
        EPISODE_SELF_IMPROVEMENT_PROMOTION_WORKFLOW_ID
    )
    assert definition is not None

    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        mod,
        "build_workflow_promotion_context",
        lambda **kwargs: {
            "success": True,
            "episode_critique_memory_id": kwargs["episode_critique_memory_id"],
            "target_workflow_id": kwargs["target_workflow_id"],
            "proposal_id": kwargs["proposal_id"],
            "workflow_authoring_proposal": {
                "proposal_id": kwargs["proposal_id"],
                "status": "pending_review",
            },
            "workflow_publication_lifecycle": {
                "workflow_id": kwargs["target_workflow_id"],
                "status": "draft",
            },
            "episode_self_improvement_suggestion": {
                "suggestion_id": kwargs["suggestion_id"],
                "target_workflow_id": kwargs["target_workflow_id"],
                "target_surface": "workflow",
                "category": "workflow_change",
                "title": "Tighten route selection",
                "rationale": "Critique evidence showed the wrong workflow was used.",
                "suggested_change": "Restrict the eligible workflow branch.",
            },
            "episode_self_improvement_benchmark_summary": {
                "benchmark_fingerprint": "bench-1",
                "benchmark_signal_summary": {"fail": 0, "pass": 3},
            },
        },
    )

    def _record(**kwargs: Any) -> dict[str, Any]:
        captured["record"] = kwargs
        return {
            "success": True,
            "proposal": {"proposal_id": kwargs["proposal_id"]},
            "promotion_evaluation": {
                "proposal_id": kwargs["proposal_id"],
                "promotion_recommendation": kwargs["promotion_recommendation"],
                "summary": kwargs["summary"],
            },
        }

    monkeypatch.setattr(mod, "record_workflow_promotion_evaluation", _record)

    registry = ActionRegistry()
    register_episode_self_improvement_actions(registry)

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=_QueuedLLM(
                [
                    json.dumps(
                        {
                            "promotion_recommendation": "ready_for_review",
                            "summary": "Proposal is structurally sound.",
                            "reasoning": "The revised branch narrows the failure mode.",
                            "required_follow_up": ["human review"],
                            "approval_ready": True,
                        }
                    )
                ]
            ),
            user_namespace="#V#user@org",
        ),
        data={
            "episode_critique_memory_id": "#V#episode_critique_memory_1",
            "target_workflow_id": "#V#alpha_workflow",
            "proposal_id": "proposal-1",
            "suggestion_id": "workflow_change_alpha",
            "namespace": "#V#user@org",
        },
    )

    assert result.completed is True
    assert result.error is None
    assert result.data["promotion_evaluation"]["proposal_id"] == "proposal-1"
    assert (
        result.data["promotion_evaluation"]["promotion_recommendation"]
        == "ready_for_review"
    )
    assert captured["record"]["proposal_id"] == "proposal-1"
    assert captured["record"]["promotion_recommendation"] == "ready_for_review"
    assert captured["record"]["benchmark_summary"]["benchmark_fingerprint"] == "bench-1"
