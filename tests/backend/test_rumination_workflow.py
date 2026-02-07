"""Tests for the rumination orchestrator workflow (JVNAUTOSCI-923).

Tests the assess → plan → dispatch → complete state machine and its
budget-aware enrichment dispatching.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_mock_db(monkeypatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    from src.backend.db.mongo_client import get_db
    from src.backend.workflows.durable import instance_manager

    db = get_db()
    if db is not None:
        try:
            db.drop_collection("workflow_instances")
            db.drop_collection("workflow_schedules")
        except Exception:
            pass

    instance_manager._indexes_ensured = False
    yield


class TestRuminationWorkflowDefinition:
    """Test workflow definition structure."""

    def test_workflow_structure(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            build_rumination_workflow,
            RUMINATION_WORKFLOW_ID,
        )

        wf = build_rumination_workflow()
        assert wf.workflow_id == RUMINATION_WORKFLOW_ID
        assert wf.initial_state == "assess"
        assert "assess" in wf.states
        assert "plan" in wf.states
        assert "dispatch" in wf.states
        assert "complete" in wf.states
        assert "failed" in wf.states

    def test_terminal_states(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            build_rumination_workflow,
        )

        wf = build_rumination_workflow()
        assert wf.states["complete"].terminal is True
        assert wf.states["failed"].terminal is True
        assert wf.states["assess"].terminal is False

    def test_assess_transitions(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            build_rumination_workflow,
        )

        wf = build_rumination_workflow()
        transitions = wf.states["assess"].transitions
        to_states = [t.to_state for t in transitions]
        assert "plan" in to_states
        assert "complete" in to_states

        # With gaps: should go to plan
        ctx_with_gaps = {"gap_assessment": {"missing_descriptions": 5}}
        assert transitions[0].condition(ctx_with_gaps) is True

        # Without gaps: should go to complete
        ctx_no_gaps = {"gap_assessment": {"missing_descriptions": 0}}
        assert transitions[0].condition(ctx_no_gaps) is False
        assert transitions[1].condition(ctx_no_gaps) is True


class TestRuminationAssessGaps:
    """Test the gap assessment handler."""

    def test_assess_detects_missing_predicates(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.rumination_workflow import (
            _handle_assess_gaps,
        )

        with (
            patch(
                "src.backend.workflows.durable.rumination_workflow."
                "_count_concepts_missing_predicate"
            ) as mock_count,
            patch(
                "src.backend.workflows.durable.rumination_workflow."
                "_count_isolated_concepts",
                return_value=3,
            ),
        ):
            mock_count.side_effect = [15, 8]  # descriptions, considerations

            env = WorkflowEnvironment(llm_client=None)
            req = WorkflowActionRequest(
                action_id="rumination.assess_gaps",
                inputs={},
                environment=env,
                data={},
            )

            result = _handle_assess_gaps(req)
            assert result.ok
            assessment = result.outputs["gap_assessment"]
            assert assessment["missing_descriptions"] == 15
            assert assessment["missing_considerations"] == 8
            assert assessment["isolated_concepts"] == 3

    def test_assess_empty_ontology(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.rumination_workflow import (
            _handle_assess_gaps,
        )

        with (
            patch(
                "src.backend.workflows.durable.rumination_workflow."
                "_count_concepts_missing_predicate",
                return_value=0,
            ),
            patch(
                "src.backend.workflows.durable.rumination_workflow."
                "_count_isolated_concepts",
                return_value=0,
            ),
        ):
            env = WorkflowEnvironment(llm_client=None)
            req = WorkflowActionRequest(
                action_id="rumination.assess_gaps",
                inputs={},
                environment=env,
                data={},
            )

            result = _handle_assess_gaps(req)
            assert result.ok
            # All zeros → orchestrator should transition to complete, not plan
            assessment = result.outputs["gap_assessment"]
            assert all(v == 0 for v in assessment.values())


class TestRuminationPlanEnrichment:
    """Test the budget-aware planning handler."""

    def test_plan_allocates_budget_by_priority(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.rumination_workflow import (
            _handle_plan_enrichment,
        )

        env = WorkflowEnvironment(llm_client=None)
        req = WorkflowActionRequest(
            action_id="rumination.plan_enrichment",
            inputs={},
            environment=env,
            data={
                "budget": 20,
                "gap_assessment": {
                    "missing_descriptions": 15,
                    "missing_considerations": 8,
                },
                "gap_dimensions": [
                    {
                        "gap_name": "missing_descriptions",
                        "predicate": "hasDescription",
                        "priority": 1,
                    },
                    {
                        "gap_name": "missing_considerations",
                        "predicate": "#V#has_considerations_for_use",
                        "priority": 2,
                    },
                ],
            },
        )

        result = _handle_plan_enrichment(req)
        assert result.ok
        plan = result.outputs["enrichment_plan"]

        # With budget 20: descriptions gets 15, considerations gets 5 (remaining)
        assert len(plan) == 2
        assert plan[0]["gap_name"] == "missing_descriptions"
        assert plan[0]["allocation"] == 15
        assert plan[1]["gap_name"] == "missing_considerations"
        assert plan[1]["allocation"] == 5

    def test_plan_respects_budget_limit(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.rumination_workflow import (
            _handle_plan_enrichment,
        )

        env = WorkflowEnvironment(llm_client=None)
        req = WorkflowActionRequest(
            action_id="rumination.plan_enrichment",
            inputs={},
            environment=env,
            data={
                "budget": 5,
                "gap_assessment": {
                    "missing_descriptions": 100,
                    "missing_considerations": 50,
                },
                "gap_dimensions": [
                    {
                        "gap_name": "missing_descriptions",
                        "predicate": "hasDescription",
                        "priority": 1,
                    },
                    {
                        "gap_name": "missing_considerations",
                        "predicate": "#V#has_considerations_for_use",
                        "priority": 2,
                    },
                ],
            },
        )

        result = _handle_plan_enrichment(req)
        assert result.ok
        plan = result.outputs["enrichment_plan"]

        # Budget 5 should all go to highest priority
        total_allocated = sum(t["allocation"] for t in plan)
        assert total_allocated == 5

    def test_plan_skips_zero_gap_dimensions(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.rumination_workflow import (
            _handle_plan_enrichment,
        )

        env = WorkflowEnvironment(llm_client=None)
        req = WorkflowActionRequest(
            action_id="rumination.plan_enrichment",
            inputs={},
            environment=env,
            data={
                "budget": 20,
                "gap_assessment": {
                    "missing_descriptions": 0,
                    "missing_considerations": 5,
                },
                "gap_dimensions": [
                    {
                        "gap_name": "missing_descriptions",
                        "predicate": "hasDescription",
                        "priority": 1,
                    },
                    {
                        "gap_name": "missing_considerations",
                        "predicate": "#V#has_considerations_for_use",
                        "priority": 2,
                    },
                ],
            },
        )

        result = _handle_plan_enrichment(req)
        assert result.ok
        plan = result.outputs["enrichment_plan"]

        # Only considerations should appear (descriptions has 0 gaps)
        assert len(plan) == 1
        assert plan[0]["gap_name"] == "missing_considerations"


class TestRuminationDispatch:
    """Test the dispatch handler."""

    def test_dispatch_calls_enrichment(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.rumination_workflow import (
            _handle_dispatch_enrichment,
        )

        mock_llm = MagicMock()
        mock_llm.generate.return_value = "Generated description text here."

        with (
            patch(
                "src.backend.workflows.durable.enrichment_workflow."
                "_find_concepts_missing_predicate",
                return_value=["#V#c1", "#V#c2"],
            ),
            patch(
                "src.backend.workflows.durable.enrichment_workflow."
                "_resolve_prompt_template",
                return_value="Generate for {concept_name}: {predicate}",
            ),
            patch(
                "src.backend.languagemodels.llm_interface.get_llm_client",
                return_value=mock_llm,
            ),
            patch(
                "src.backend.db.repositories.concepts_repository.ConceptsRepository"
            ) as mock_repo,
            patch(
                "src.backend.workflows.durable.enrichment_workflow."
                "_build_concept_context",
                return_value={
                    "concept_name": "Test",
                    "concept_id": "#V#test",
                    "type_hierarchy": "",
                    "relationships": "",
                    "description": "",
                },
            ),
            patch(
                "src.backend.workflows.durable.enrichment_workflow."
                "_upsert_text_value_internal"
            ) as mock_upsert,
        ):
            mock_repo.find_one.return_value = {"concept_id": "#V#c1"}

            env = WorkflowEnvironment(llm_client=mock_llm)
            req = WorkflowActionRequest(
                action_id="rumination.dispatch_enrichment",
                inputs={},
                environment=env,
                data={
                    "enrichment_plan": [
                        {
                            "gap_name": "missing_descriptions",
                            "predicate": "hasDescription",
                            "allocation": 5,
                        },
                    ],
                    "plan_index": 0,
                    "dispatched_tasks": [],
                    "total_processed": 0,
                    "total_failed": 0,
                },
            )

            result = _handle_dispatch_enrichment(req)
            assert result.ok
            assert result.outputs["total_processed"] >= 0
            assert result.outputs["plan_index"] == 1
            assert result.outputs["has_more_tasks"] is False

    def test_dispatch_past_end_of_plan(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.rumination_workflow import (
            _handle_dispatch_enrichment,
        )

        env = WorkflowEnvironment(llm_client=None)
        req = WorkflowActionRequest(
            action_id="rumination.dispatch_enrichment",
            inputs={},
            environment=env,
            data={
                "enrichment_plan": [],
                "plan_index": 0,
                "dispatched_tasks": [],
                "total_processed": 0,
                "total_failed": 0,
            },
        )

        result = _handle_dispatch_enrichment(req)
        assert result.ok
        assert result.outputs["has_more_tasks"] is False


class TestRuminationFinalise:
    """Test the finalise handler."""

    def test_finalise_produces_summary(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.rumination_workflow import (
            _handle_finalise,
        )

        env = WorkflowEnvironment(llm_client=None)
        req = WorkflowActionRequest(
            action_id="rumination.finalise",
            inputs={},
            environment=env,
            data={
                "gap_assessment": {"missing_descriptions": 10},
                "dispatched_tasks": [
                    {"gap_name": "missing_descriptions", "processed": 8, "failed": 2}
                ],
                "total_processed": 8,
                "total_failed": 2,
            },
        )

        result = _handle_finalise(req)
        assert result.ok
        summary = result.outputs["rumination_result"]
        assert summary["total_processed"] == 8
        assert summary["total_failed"] == 2
        assert summary["success"] is False  # failed > 0
        assert summary["tasks_dispatched"] == 1


class TestRuminationRegistration:
    """Test registration in the unified registry."""

    def test_workflow_registration(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            get_rumination_workflow_registration,
            RUMINATION_WORKFLOW_ID,
        )

        reg = get_rumination_workflow_registration()
        assert reg.workflow_id == RUMINATION_WORKFLOW_ID
        assert reg.source == "built_in"

    def test_action_registration(self) -> None:
        from src.backend.workflows.action_registry import ActionRegistry
        from src.backend.workflows.durable.rumination_workflow import (
            register_rumination_actions,
        )

        registry = ActionRegistry()
        register_rumination_actions(registry)

        expected = [
            "rumination.assess_gaps",
            "rumination.plan_enrichment",
            "rumination.dispatch_enrichment",
            "rumination.finalise",
        ]
        for action_id in expected:
            assert registry.has(action_id), f"Missing: {action_id}"

    def test_registered_in_factory(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            RUMINATION_WORKFLOW_ID,
        )

        with patch(
            "src.backend.workflows.durable.registry_factory.discover_workflow_ids",
            return_value=[],
        ):
            from src.backend.workflows.durable.registry_factory import (
                build_workflow_registry,
            )

            registry = build_workflow_registry()
            assert RUMINATION_WORKFLOW_ID in registry.all_workflow_ids()
