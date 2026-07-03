"""Pipeline stage coherence tests (JVNAUTOSCI-1811).

Cross-stage contract tests verifying that discovery candidates survive into
the selector, and that dispatch identity is attributed to the primary
workflow rather than post-processing workflows.
"""

from __future__ import annotations

from typing import Any, Mapping, cast

import pytest

from src.backend.services.turn_execution_record_service import (
    _summarise_tool_execution_context,
)
from src.backend.workflows import (
    LazyWorkflowRegistration,
    WorkflowDefinition,
    WorkflowRegistration,
    WorkflowStateSpec,
)
from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
)
from src.backend.workflows.workflow_registry import WorkflowRegistry
from orchestrator_test_harness import build_db_independent_orchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}

    def invoke(self, _tool_name: str, _payload: Mapping[str, Any]):
        raise AssertionError("Gateway should not be invoked in this test")


def _build_orchestrator(monkeypatch: pytest.MonkeyPatch):
    return build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _StubGateway()),
        selector_enabled=True,
        max_tool_invocations=1,
    )


# ---------------------------------------------------------------------------
# Bug 1: Discovery-to-selector candidate passthrough (JVNAUTOSCI-1808)
# ---------------------------------------------------------------------------


class TestDiscoverySelectorPassthrough:
    """Verify that discovered candidates not pre-registered survive into the
    selector via JIT lazy registration."""

    def test_discovered_candidate_jit_registered_passes_filter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A discovered, executable candidate that is NOT pre-registered
        should be JIT lazy-registered and included (not excluded)."""
        orchestrator = _build_orchestrator(monkeypatch)

        # Confirm the candidate is NOT already in the registry.
        assert not orchestrator._workflow_registry.has(
            "#V#arxiv_paper_ingestion_workflow"
        )

        discovery_result = {
            "candidates": [
                {
                    "concept_id": "#V#arxiv_paper_ingestion_workflow",
                    "confidence": 1.0,
                    "description": "Ingest arXiv paper",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "routing_eligible": True,
                }
            ]
        }

        included, excluded = orchestrator._prepare_selector_discovered_matches(
            discovery_result, turn_text="ingest https://arxiv.org/abs/2402.18144"
        )

        assert len(included) == 1
        assert included[0]["concept_id"] == "#V#arxiv_paper_ingestion_workflow"
        assert included[0]["is_policy_safe"] is True

        # The workflow should now be lazy-registered.
        assert orchestrator._workflow_registry.has(
            "#V#arxiv_paper_ingestion_workflow"
        )

    def test_discovered_candidate_already_registered_still_included(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-registered workflow should still pass through normally."""
        orchestrator = _build_orchestrator(monkeypatch)

        # Pre-register the workflow.
        orchestrator._workflow_registry.register_or_replace(
            WorkflowRegistration(
                workflow_id="#V#test_workflow",
                definition=WorkflowDefinition(
                    workflow_id="#V#test_workflow",
                    initial_state="done",
                    states={
                        "done": WorkflowStateSpec(
                            state_id="done", actions=(), terminal=True
                        )
                    },
                ),
                purpose="Test workflow",
                source="test",
            )
        )

        discovery_result = {
            "candidates": [
                {
                    "concept_id": "#V#test_workflow",
                    "confidence": 0.9,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "routing_eligible": True,
                }
            ]
        }

        included, excluded = orchestrator._prepare_selector_discovered_matches(
            discovery_result, turn_text="test"
        )

        assert len(included) == 1
        assert included[0]["concept_id"] == "#V#test_workflow"

    def test_non_executable_candidate_excluded_even_with_jit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-executable candidate should still be excluded regardless of
        JIT registration."""
        orchestrator = _build_orchestrator(monkeypatch)

        discovery_result = {
            "candidates": [
                {
                    "concept_id": "#V#design_artifact_workflow",
                    "confidence": 0.8,
                    "is_executable": False,
                    "executability_reason": "non_executable_design_artifact",
                    "routing_eligible": True,
                }
            ]
        }

        included, excluded = orchestrator._prepare_selector_discovered_matches(
            discovery_result, turn_text="test"
        )

        assert len(included) == 0
        assert len(excluded) == 1
        assert "non_executable" in excluded[0].get("routing_exclusion_reason", "")

    def test_missing_routing_profile_does_not_materialise_lazy_candidate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orchestrator = _build_orchestrator(monkeypatch)

        def _unexpected_lazy_load(workflow_id: str) -> WorkflowDefinition:
            raise AssertionError(f"selector preparation loaded {workflow_id}")

        registry = WorkflowRegistry(definition_loader=_unexpected_lazy_load)
        registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id="#V#lazy_discovered_workflow",
                purpose="Lazy discovered workflow",
                source="vontology",
            )
        )
        orchestrator._workflow_registry = registry

        included, excluded = orchestrator._prepare_selector_discovered_matches(
            {
                "candidates": [
                    {
                        "concept_id": "#V#lazy_discovered_workflow",
                        "description": "Lazy discovered workflow",
                        "is_executable": True,
                        "executability_reason": "executable_now",
                        "routing_eligible": True,
                    }
                ]
            },
            turn_text="use the lazy discovered workflow",
        )

        assert [item["concept_id"] for item in included] == [
            "#V#lazy_discovered_workflow"
        ]
        assert excluded == []


class TestSelectorDefaultCandidates:
    def test_default_candidates_do_not_materialise_lazy_workflows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orchestrator = _build_orchestrator(monkeypatch)

        def _unexpected_lazy_load(workflow_id: str) -> WorkflowDefinition:
            raise AssertionError(f"default candidate builder loaded {workflow_id}")

        registry = WorkflowRegistry(definition_loader=_unexpected_lazy_load)
        for workflow_id in (
            CHAT_ASSISTANT_WORKFLOW_ID,
            TOOL_CALLING_WORKFLOW_ID,
            CHAT_NARRATION_WORKFLOW_ID,
        ):
            registry.register_lazy(
                LazyWorkflowRegistration(
                    workflow_id=workflow_id,
                    purpose=f"Purpose for {workflow_id}",
                    source="vontology",
                )
            )
        orchestrator._workflow_registry = registry

        candidates, excluded = orchestrator._build_selector_default_candidates()

        assert [item["concept_id"] for item in candidates] == [
            CHAT_ASSISTANT_WORKFLOW_ID,
            TOOL_CALLING_WORKFLOW_ID,
            CHAT_NARRATION_WORKFLOW_ID,
        ]
        assert excluded == []
        assert candidates[1]["description"] == f"Purpose for {TOOL_CALLING_WORKFLOW_ID}"


# ---------------------------------------------------------------------------
# Bug 2: Dispatch identity divergence (JVNAUTOSCI-1809)
# ---------------------------------------------------------------------------


class TestDispatchIdentityAttribution:
    """Verify that the turn execution summary attributes dispatch identity
    to the primary workflow, not post-processing workflows like buttonify."""

    def test_primary_workflow_submission_preferred_over_buttonify(self) -> None:
        """When both primary and buttonify submission events exist, the summary
        should use the primary workflow's submission."""
        summary = _summarise_tool_execution_context(
            workflow_routing={
                "workflow_id": "#V#chat_assistant_workflow",
                "verdict": "plain_response",
            },
            turn_execution_diagnostics=None,
            aux_llm_calls=[
                {
                    "type": "workflow_instance_submission",
                    "workflow_id": "#V#chat_assistant_workflow",
                    "status": "submission_ok",
                },
                {
                    "type": "workflow_instance_submission",
                    "workflow_id": "#V#chat_buttonify_workflow",
                    "status": "submission_ok",
                },
            ],
            serialised_invocations=[],
        )

        # dispatch_workflow_id should NOT be buttonify.
        assert summary.get("dispatch_workflow_id") != "#V#chat_buttonify_workflow"

    def test_submission_failed_uses_primary_not_buttonify(self) -> None:
        """When the primary workflow submission failed and buttonify ran after,
        the failure should be attributed to the primary workflow."""
        summary = _summarise_tool_execution_context(
            workflow_routing={
                "workflow_id": "#V#chat_assistant_workflow",
                "verdict": "plain_response",
            },
            turn_execution_diagnostics=None,
            aux_llm_calls=[
                {
                    "type": "workflow_instance_submission",
                    "workflow_id": "#V#chat_assistant_workflow",
                    "status": "submission_failed",
                    "reason_code": "workflow_not_runnable",
                    "submission": {
                        "verification": {"runnable_verification_success": False},
                    },
                },
                {
                    "type": "workflow_instance_submission",
                    "workflow_id": "#V#chat_buttonify_workflow",
                    "status": "submission_ok",
                },
            ],
            serialised_invocations=[],
        )

        # The fallback code should set dispatch_workflow_id from the primary,
        # not from buttonify.
        assert (
            summary.get("dispatch_workflow_id") == "#V#chat_assistant_workflow"
        ), (
            f"Expected primary workflow, got: {summary.get('dispatch_workflow_id')}"
        )
        assert summary.get("dispatch_terminal_status") == "failed"

    def test_single_submission_event_still_works(self) -> None:
        """A single submission event (no buttonify) should still be handled."""
        summary = _summarise_tool_execution_context(
            workflow_routing={
                "workflow_id": "#V#chat_assistant_workflow",
                "verdict": "plain_response",
            },
            turn_execution_diagnostics=None,
            aux_llm_calls=[
                {
                    "type": "workflow_instance_submission",
                    "workflow_id": "#V#chat_assistant_workflow",
                    "status": "submission_ok",
                },
            ],
            serialised_invocations=[],
        )

        # Should work normally.
        assert summary.get("dispatch_workflow_id") != "#V#chat_buttonify_workflow"


# ---------------------------------------------------------------------------
# JIT lazy registration unit-level tests
# ---------------------------------------------------------------------------


class TestWorkflowRegistryJitLazy:
    """Verify that register_lazy correctly extends registry membership."""

    def test_lazy_registration_makes_has_return_true(self) -> None:
        registry = WorkflowRegistry()
        assert not registry.has("#V#new_workflow")
        registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id="#V#new_workflow",
                purpose="Test",
                source="jit_discovery",
            )
        )
        assert registry.has("#V#new_workflow")
        assert "#V#new_workflow" in set(registry.all_workflow_ids())

    def test_lazy_registration_skipped_for_eager(self) -> None:
        registry = WorkflowRegistry()
        registry.register_or_replace(
            WorkflowRegistration(
                workflow_id="#V#existing",
                definition=WorkflowDefinition(
                    workflow_id="#V#existing",
                    initial_state="done",
                    states={
                        "done": WorkflowStateSpec(
                            state_id="done", actions=(), terminal=True
                        )
                    },
                ),
                purpose="Existing",
                source="test",
            )
        )
        stored = registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id="#V#existing",
                purpose="Duplicate",
                source="jit_discovery",
            )
        )
        assert stored is False
