"""Tests for the rumination orchestrator workflow (JVNAUTOSCI-923).

Tests the assess → plan → dispatch → complete state machine and its
budget-aware enrichment dispatching.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _build_relation_policy_context(
    *,
    profile_concept_id: str | None = (
        "#V#knowledge_acquisition_profile_low_imposition_relation_completion"
    ),
    question_limit: int = 1,
    detail_limit: int = 80,
    priority_policy: dict | None = None,
    auto_apply_policy: dict | None = None,
    error: str | None = None,
) -> dict:
    return {
        "profile_concept_id": profile_concept_id,
        "relation_candidate_priority_policy": priority_policy
        if priority_policy is not None
        else {
            "policy_version": (
                "knowledge_acquisition_profile.relation_candidate_priority.v1"
            ),
            "default_priority": 0,
            "predicate_priorities": {
                "#V#has_affiliation": 80,
                "#V#works_on_project": 76,
                "#V#depends_on": 74,
                "#V#related_to": 10,
            },
        },
        "relation_auto_apply_policy": auto_apply_policy
        if auto_apply_policy is not None
        else {
            "policy_version": "knowledge_acquisition_profile.v2",
            "default_threshold": 0.95,
            "source_adjustments": {
                "human_validated": 0.08,
                "user_confirmed": 0.06,
                "explicit_user_input": 0.05,
                "llm_extraction": 0.0,
                "heuristic_inference": -0.05,
                "unknown": 0.0,
            },
            "min_evidence_count": 1,
            "predicate_policies": {
                "#V#has_affiliation": {"threshold": 0.96},
                "#V#works_on_project": {"threshold": 0.96},
                "#V#depends_on": {"threshold": 0.98},
                "#V#related_to": {"threshold": 0.95},
            },
        },
        "question_limit": question_limit,
        "detail_limit": detail_limit,
        **({"error": error} if error else {}),
    }


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
            build_rumination_workflow_test_definition,
            RUMINATION_WORKFLOW_ID,
        )

        wf = build_rumination_workflow_test_definition()
        assert wf.workflow_id == RUMINATION_WORKFLOW_ID
        assert wf.initial_state == "assess"
        assert "assess" in wf.states
        assert "plan" in wf.states
        assert "dispatch" in wf.states
        assert "complete" in wf.states
        assert "failed" in wf.states

    def test_terminal_states(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            build_rumination_workflow_test_definition,
        )

        wf = build_rumination_workflow_test_definition()
        assert wf.states["complete"].terminal is True
        assert wf.states["failed"].terminal is True
        assert wf.states["assess"].terminal is False

    def test_assess_transitions(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            build_rumination_workflow_test_definition,
        )

        wf = build_rumination_workflow_test_definition()
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

    def test_assess_includes_relation_gap_candidates(self) -> None:
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
            patch(
                "src.backend.workflows.durable.rumination_workflow."
                "_list_relation_gap_candidates",
                return_value=[
                    {
                        "concept_id": "#V#alice",
                        "missing_predicates": ["#V#has_affiliation"],
                    },
                    {
                        "concept_id": "#V#bob",
                        "missing_predicates": ["#V#depends_on"],
                    },
                ],
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
            assessment = result.outputs["gap_assessment"]
            assert assessment["missing_relations"] == 2
            assert len(result.outputs["relation_gap_candidates"]) == 2

    def test_assess_relation_gap_candidates_fail_closed_on_invalid_profile(self) -> None:
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
            patch(
                "src.backend.workflows.durable.rumination_workflow."
                "_resolve_relation_policy_input",
                return_value=_build_relation_policy_context(
                    profile_concept_id=None,
                    error="knowledge_acquisition_profile_invalid",
                ),
            ),
            patch(
                "src.backend.workflows.durable.rumination_workflow."
                "_list_relation_gap_candidates"
            ) as mock_list_candidates,
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
        assessment = result.outputs["gap_assessment"]
        assert assessment["missing_relations"] == -1
        summary = result.outputs["relation_gap_summary"]["missing_relations"]
        assert summary["error"] == "knowledge_acquisition_profile_invalid"
        mock_list_candidates.assert_not_called()


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
                return_value=(
                    "Generate for {concept_name}: {predicate}",
                    {
                        "source": "inline_template",
                        "prompt_concept_id": None,
                        "available": True,
                        "error": None,
                    },
                ),
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
            ),
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

    def test_dispatch_relation_completion_dry_run(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.rumination_workflow import (
            _handle_dispatch_enrichment,
        )

        with patch(
            "src.backend.workflows.durable.rumination_workflow."
            "_dispatch_relation_completion_task",
            return_value={
                "metrics": {
                    "concepts_considered": 1,
                    "proposed_relations": 2,
                    "auto_applied_relations": 0,
                    "would_apply_relations": 1,
                    "deferred_relations": 1,
                    "confirmed_relations": 0,
                    "failed_relations": 0,
                },
                "proposal_details": [
                    {
                        "source_id": "#V#alice",
                        "predicate": "#V#has_affiliation",
                        "action": "would_auto_apply",
                    }
                ],
                "deferred_questions": [
                    {
                        "source_id": "#V#alice",
                        "predicate": "#V#depends_on",
                        "question": "What should the dependency relation be?",
                    }
                ],
            },
        ):
            env = WorkflowEnvironment(llm_client=None)
            req = WorkflowActionRequest(
                action_id="rumination.dispatch_enrichment",
                inputs={},
                environment=env,
                data={
                    "dry_run": True,
                    "enrichment_plan": [
                        {
                            "gap_name": "missing_relations",
                            "predicate": "__relation_completion__",
                            "dispatch_mode": "relation_completion",
                            "allocation": 1,
                        }
                    ],
                    "plan_index": 0,
                    "dispatched_tasks": [],
                    "total_processed": 0,
                    "total_failed": 0,
                    "relation_metrics": {},
                    "relation_changes": [],
                    "relation_questions": [],
                },
            )

            result = _handle_dispatch_enrichment(req)
            assert result.ok
            assert result.outputs["has_more_tasks"] is False
            metrics = result.outputs["relation_metrics"]
            assert metrics["proposed_relations"] == 2
            assert metrics["would_apply_relations"] == 1
            assert metrics["deferred_relations"] == 1
            assert len(result.outputs["relation_changes"]) == 1
            assert len(result.outputs["relation_questions"]) == 1


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

    def test_finalise_includes_relation_metrics(self) -> None:
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
                "gap_assessment": {"missing_relations": 1},
                "dispatched_tasks": [{"gap_name": "missing_relations"}],
                "total_processed": 0,
                "total_failed": 0,
                "relation_metrics": {"proposed_relations": 3, "deferred_relations": 2},
                "relation_changes": [{"source_id": "#V#alice"}],
                "relation_questions": [{"source_id": "#V#alice"}],
            },
        )

        result = _handle_finalise(req)
        assert result.ok
        summary = result.outputs["rumination_result"]
        assert summary["relation_metrics"]["proposed_relations"] == 3
        assert summary["relation_changes_count"] == 1
        assert summary["relation_questions_count"] == 1


class TestRuminationRelationCompletionHelpers:
    def test_list_relation_gap_candidates_uses_priority_policy(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            _list_relation_gap_candidates,
        )

        concept_docs = [
            {"concept_id": "#V#alice", "name": "Alice", "relationships": {}},
            {"concept_id": "#V#bob", "name": "Bob", "relationships": {}},
        ]
        predicate_by_concept = {
            "#V#alice": ["#V#has_affiliation"],
            "#V#bob": ["#V#related_to"],
        }

        def _fake_get_elicitation_opportunities(
            instance_id: str,
            include_reverse_subtypes: bool = True,
            include_hypothesized: bool = False,
        ):
            return predicate_by_concept[instance_id]

        with (
            patch(
                "src.backend.db.repositories.concepts_repository.ConceptsRepository.find",
                return_value=concept_docs,
            ),
            patch(
                "src.backend.services.relation_elicitation_service."
                "RelationElicitationService.get_elicitation_opportunities",
                side_effect=_fake_get_elicitation_opportunities,
            ),
            patch(
                "src.backend.vontology.utils_vontology."
                "get_concept_display_name_with_names_fallback",
                side_effect=lambda concept_doc: concept_doc.get("name"),
            ),
        ):
            affiliation_first = _list_relation_gap_candidates(
                scan_limit=10,
                max_predicates_per_concept=1,
                priority_policy_input={
                    "policy_version": (
                        "knowledge_acquisition_profile.relation_candidate_priority.v1"
                    ),
                    "default_priority": 0,
                    "predicate_priorities": {
                        "#V#has_affiliation": 80,
                        "#V#related_to": 10,
                    },
                },
            )
            related_first = _list_relation_gap_candidates(
                scan_limit=10,
                max_predicates_per_concept=1,
                priority_policy_input={
                    "policy_version": (
                        "knowledge_acquisition_profile.relation_candidate_priority.v1"
                    ),
                    "default_priority": 0,
                    "predicate_priorities": {
                        "#V#has_affiliation": 5,
                        "#V#related_to": 90,
                    },
                },
            )

        assert affiliation_first[0]["concept_id"] == "#V#alice"
        assert affiliation_first[0]["priority_policy_matches"] == ["#V#has_affiliation"]
        assert related_first[0]["concept_id"] == "#V#bob"
        assert related_first[0]["priority_policy_matches"] == ["#V#related_to"]

    def test_dispatch_relation_completion_limits_questions_via_profile(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            _dispatch_relation_completion_task,
        )

        instance_doc = {
            "concept_id": "#V#alice",
            "relationships": {},
            "hypothesized_relations": {},
            "name": "Alice",
        }

        with (
            patch(
                "src.backend.workflows.durable.rumination_workflow."
                "_resolve_relation_policy_input",
                return_value=_build_relation_policy_context(
                    question_limit=1,
                    detail_limit=10,
                ),
            ),
            patch(
                "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
                return_value=instance_doc,
            ),
            patch(
                "src.backend.services.relation_elicitation_service."
                "RelationElicitationService.generate_question_for_elicit",
                side_effect=[
                    "What is Alice's affiliation?",
                    "What project is Alice working on?",
                ],
            ),
        ):
            result = _dispatch_relation_completion_task(
                task={
                    "gap_name": "missing_relations",
                    "predicate": "__relation_completion__",
                    "allocation": 1,
                },
                ctx={
                    "dry_run": True,
                    "relation_gap_candidates": [
                        {
                            "concept_id": "#V#alice",
                            "missing_predicates": [
                                "#V#has_affiliation",
                                "#V#works_on_project",
                            ],
                        }
                    ],
                },
            )

        assert result["metrics"]["deferred_relations"] == 2
        assert len(result["deferred_questions"]) == 1
        assert result["policy_profile_concept_id"] == (
            "#V#knowledge_acquisition_profile_low_imposition_relation_completion"
        )

    def test_dispatch_relation_completion_reports_profile_resolution_failure(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            _dispatch_relation_completion_task,
        )

        with patch(
            "src.backend.workflows.durable.rumination_workflow."
            "_resolve_relation_policy_input",
            return_value=_build_relation_policy_context(
                profile_concept_id=None,
                priority_policy={},
                auto_apply_policy={},
                question_limit=1,
                detail_limit=10,
                error="knowledge_acquisition_profile_unavailable",
            ),
        ):
            result = _dispatch_relation_completion_task(
                task={
                    "gap_name": "missing_relations",
                    "predicate": "__relation_completion__",
                    "allocation": 1,
                },
                ctx={
                    "dry_run": True,
                    "relation_gap_candidates": [],
                },
            )

        assert result["policy_error"] == "knowledge_acquisition_profile_unavailable"
        assert result["metrics"]["failed_relations"] == 1

    def test_dispatch_relation_completion_auto_apply_dry_run(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            _dispatch_relation_completion_task,
        )

        instance_doc = {
            "concept_id": "#V#alice",
            "relationships": {},
            "hypothesized_relations": {
                "#V#has_affiliation": [
                    {"value": "#V#strong_ai_lab", "confidence_score": 0.99}
                ]
            },
            "name": "Alice",
        }

        def _fake_find_one(query, projection=None):
            cid = (query or {}).get("concept_id")
            if cid == "#V#alice":
                return instance_doc
            if cid == "#V#strong_ai_lab":
                return {"concept_id": "#V#strong_ai_lab"}
            return None

        with (
            patch(
                "src.backend.workflows.durable.rumination_workflow."
                "_resolve_relation_policy_input",
                return_value=_build_relation_policy_context(),
            ),
            patch(
                "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
                side_effect=_fake_find_one,
            ),
            patch(
                "src.backend.services.relation_elicitation_service."
                "RelationElicitationService.generate_question_for_elicit",
                return_value="What is Alice's affiliation?",
            ),
            patch(
                "src.backend.services.relationship_write_service.add_relationship"
            ) as mock_add_relationship,
        ):
            result = _dispatch_relation_completion_task(
                task={
                    "gap_name": "missing_relations",
                    "predicate": "__relation_completion__",
                    "allocation": 1,
                },
                ctx={
                    "dry_run": True,
                    "relation_gap_candidates": [
                        {
                            "concept_id": "#V#alice",
                            "missing_predicates": ["#V#has_affiliation"],
                        }
                    ],
                },
            )

        metrics = result["metrics"]
        assert metrics["concepts_considered"] == 1
        assert metrics["proposed_relations"] == 1
        assert metrics["would_apply_relations"] == 1
        assert metrics["auto_applied_relations"] == 0
        assert metrics["deferred_relations"] == 0
        assert len(result["proposal_details"]) == 1
        assert result["proposal_details"][0]["action"] == "would_auto_apply"
        mock_add_relationship.assert_not_called()

    def test_dispatch_relation_completion_defers_below_threshold(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            _dispatch_relation_completion_task,
        )

        instance_doc = {
            "concept_id": "#V#alice",
            "relationships": {},
            "hypothesized_relations": {
                "#V#has_affiliation": [
                    {
                        "value": "#V#strong_ai_lab",
                        "confidence_score": 0.9,
                        "source": "llm_extraction",
                        "evidence_count": 2,
                    }
                ]
            },
            "name": "Alice",
        }

        def _fake_find_one(query, projection=None):
            cid = (query or {}).get("concept_id")
            if cid == "#V#alice":
                return instance_doc
            if cid == "#V#strong_ai_lab":
                return {"concept_id": "#V#strong_ai_lab"}
            return None

        with (
            patch(
                "src.backend.workflows.durable.rumination_workflow."
                "_resolve_relation_policy_input",
                return_value=_build_relation_policy_context(),
            ),
            patch(
                "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
                side_effect=_fake_find_one,
            ),
        ):
            result = _dispatch_relation_completion_task(
                task={
                    "gap_name": "missing_relations",
                    "predicate": "__relation_completion__",
                    "allocation": 1,
                },
                ctx={
                    "dry_run": True,
                    "relation_gap_candidates": [
                        {
                            "concept_id": "#V#alice",
                            "missing_predicates": ["#V#has_affiliation"],
                        }
                    ],
                },
            )

        detail = result["proposal_details"][0]
        assert detail["action"] == "deferred_question"
        assert detail["deferral_reason"] == "below_confidence_threshold"
        assert result["metrics"]["deferred_relations"] == 1
        assert (
            result["metrics"]["deferral_reason_counts"]["below_confidence_threshold"]
            == 1
        )

    def test_dispatch_relation_completion_tracks_predicate_policy_thresholds(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            _dispatch_relation_completion_task,
        )

        instance_doc = {
            "concept_id": "#V#alice",
            "relationships": {},
            "hypothesized_relations": {
                "#V#has_affiliation": [
                    {
                        "value": "#V#strong_ai_lab",
                        "confidence_score": 0.94,
                        "source": "llm_extraction",
                        "evidence_count": 2,
                    }
                ]
            },
            "name": "Alice",
        }

        def _fake_find_one(query, projection=None):
            cid = (query or {}).get("concept_id")
            if cid == "#V#alice":
                return instance_doc
            if cid == "#V#strong_ai_lab":
                return {"concept_id": "#V#strong_ai_lab"}
            return None

        low_threshold_policy = _build_relation_policy_context(
            auto_apply_policy={
                "policy_version": "knowledge_acquisition_profile.v2",
                "default_threshold": 0.95,
                "source_adjustments": {
                    "human_validated": 0.08,
                    "user_confirmed": 0.06,
                    "explicit_user_input": 0.05,
                    "llm_extraction": 0.0,
                    "heuristic_inference": -0.05,
                    "unknown": 0.0,
                },
                "min_evidence_count": 1,
                "predicate_policies": {
                    "#V#has_affiliation": {"threshold": 0.93},
                },
            }
        )
        high_threshold_policy = _build_relation_policy_context(
            auto_apply_policy={
                "policy_version": "knowledge_acquisition_profile.v2",
                "default_threshold": 0.95,
                "source_adjustments": {
                    "human_validated": 0.08,
                    "user_confirmed": 0.06,
                    "explicit_user_input": 0.05,
                    "llm_extraction": 0.0,
                    "heuristic_inference": -0.05,
                    "unknown": 0.0,
                },
                "min_evidence_count": 1,
                "predicate_policies": {
                    "#V#has_affiliation": {"threshold": 0.97},
                },
            }
        )

        with patch(
            "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
            side_effect=_fake_find_one,
        ):
            with patch(
                "src.backend.workflows.durable.rumination_workflow."
                "_resolve_relation_policy_input",
                return_value=low_threshold_policy,
            ):
                low_threshold_result = _dispatch_relation_completion_task(
                    task={
                        "gap_name": "missing_relations",
                        "predicate": "__relation_completion__",
                        "allocation": 1,
                    },
                    ctx={
                        "dry_run": True,
                        "relation_gap_candidates": [
                            {
                                "concept_id": "#V#alice",
                                "missing_predicates": ["#V#has_affiliation"],
                            }
                        ],
                    },
                )
            with patch(
                "src.backend.workflows.durable.rumination_workflow."
                "_resolve_relation_policy_input",
                return_value=high_threshold_policy,
            ):
                high_threshold_result = _dispatch_relation_completion_task(
                    task={
                        "gap_name": "missing_relations",
                        "predicate": "__relation_completion__",
                        "allocation": 1,
                    },
                    ctx={
                        "dry_run": True,
                        "relation_gap_candidates": [
                            {
                                "concept_id": "#V#alice",
                                "missing_predicates": ["#V#has_affiliation"],
                            }
                        ],
                    },
                )

        assert low_threshold_result["proposal_details"][0]["action"] == "would_auto_apply"
        assert high_threshold_result["proposal_details"][0]["action"] == "deferred_question"
        assert high_threshold_result["proposal_details"][0]["deferral_reason"] == (
            "below_confidence_threshold"
        )

    def test_dispatch_relation_completion_apply_mode_writes_and_audits(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            _dispatch_relation_completion_task,
        )

        instance_doc = {
            "concept_id": "#V#alice",
            "relationships": {},
            "hypothesized_relations": {
                "#V#has_affiliation": [
                    {
                        "id": "h-1",
                        "value": "#V#strong_ai_lab",
                        "confidence_score": 0.99,
                        "source": "human_validated",
                        "evidence_count": 3,
                    }
                ]
            },
            "name": "Alice",
        }

        def _fake_find_one(query, projection=None):
            cid = (query or {}).get("concept_id")
            if cid == "#V#alice":
                return instance_doc
            if cid == "#V#strong_ai_lab":
                return {"concept_id": "#V#strong_ai_lab"}
            return None

        with (
            patch(
                "src.backend.workflows.durable.rumination_workflow."
                "_resolve_relation_policy_input",
                return_value=_build_relation_policy_context(),
            ),
            patch(
                "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
                side_effect=_fake_find_one,
            ),
            patch(
                "src.backend.services.relationship_write_service.add_relationship",
                return_value={"success": True},
            ) as mock_add,
            patch(
                "src.backend.db.repositories.concepts_repository.ConceptsRepository.update_one"
            ) as mock_update_one,
        ):
            result = _dispatch_relation_completion_task(
                task={
                    "gap_name": "missing_relations",
                    "predicate": "__relation_completion__",
                    "allocation": 1,
                },
                ctx={
                    "dry_run": False,
                    "relation_gap_candidates": [
                        {
                            "concept_id": "#V#alice",
                            "missing_predicates": ["#V#has_affiliation"],
                        }
                    ],
                },
            )

        metrics = result["metrics"]
        assert metrics["auto_applied_relations"] == 1
        assert metrics["confirmed_relations"] == 1
        assert metrics["failed_relations"] == 0
        detail = result["proposal_details"][0]
        assert detail["action"] == "auto_applied"
        assert detail["hypothesis_id"] == "h-1"
        assert detail["policy_version"] == "knowledge_acquisition_profile.v2"
        mock_add.assert_called_once_with(
            "#V#alice",
            "#V#has_affiliation",
            "#V#strong_ai_lab",
        )
        assert mock_update_one.call_count >= 1

    def test_dispatch_relation_completion_apply_mode_persists_relationship(self) -> None:
        from src.backend.db.repositories.concepts_repository import ConceptsRepository
        from src.backend.workflows.durable.rumination_workflow import (
            _dispatch_relation_completion_task,
        )

        ConceptsRepository.delete_many(
            {"concept_id": {"$in": ["#V#alice", "#V#strong_ai_lab"]}}
        )
        ConceptsRepository.insert_one(
            {
                "concept_id": "#V#alice",
                "relationships": {},
                "hypothesized_relations": {
                    "#V#related_to": [
                        {
                            "id": "hyp-real-1",
                            "value": "#V#strong_ai_lab",
                            "confidence_score": 0.99,
                            "source": "human_validated",
                            "evidence_count": 2,
                        }
                    ]
                },
            }
        )
        ConceptsRepository.insert_one(
            {
                "concept_id": "#V#strong_ai_lab",
                "relationships": {},
            }
        )

        with patch(
            "src.backend.workflows.durable.rumination_workflow."
            "_resolve_relation_policy_input",
            return_value=_build_relation_policy_context(),
        ):
            result = _dispatch_relation_completion_task(
                task={
                    "gap_name": "missing_relations",
                    "predicate": "__relation_completion__",
                    "allocation": 1,
                },
                ctx={
                    "dry_run": False,
                    "relation_gap_candidates": [
                        {
                            "concept_id": "#V#alice",
                            "missing_predicates": ["#V#related_to"],
                        }
                    ],
                },
            )

        metrics = result["metrics"]
        assert metrics["auto_applied_relations"] == 1
        assert metrics["confirmed_relations"] == 1

        source_doc = ConceptsRepository.find_one({"concept_id": "#V#alice"})
        relationships = (source_doc or {}).get("relationships") or {}
        assert "#V#strong_ai_lab" in (relationships.get("related_to") or [])

        audit_log = (source_doc or {}).get("relationship_auto_apply_audit") or []
        assert isinstance(audit_log, list) and len(audit_log) > 0
        assert audit_log[-1]["hypothesis_id"] == "hyp-real-1"
        assert audit_log[-1]["policy_version"] == "knowledge_acquisition_profile.v2"


class TestRuminationRegistration:
    """Test registration in the unified registry."""

    def test_workflow_registration(self) -> None:
        from src.backend.workflows.durable.rumination_workflow import (
            build_rumination_workflow_test_registration,
            RUMINATION_WORKFLOW_ID,
        )

        reg = build_rumination_workflow_test_registration()
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

    def test_runtime_registry_does_not_register_python_test_definition(self) -> None:
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
            assert RUMINATION_WORKFLOW_ID not in registry.all_workflow_ids()
