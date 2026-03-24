"""Tests for the parameterised enrichment workflow (JVNAUTOSCI-923).

Tests the workflow definition structure, action handlers, and registration.
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


class TestEnrichmentWorkflowDefinition:
    """Test that the workflow definition is structurally valid."""

    def test_workflow_structure(self) -> None:
        from src.backend.workflows.durable.enrichment_workflow import (
            build_enrichment_workflow_test_definition,
            ENRICHMENT_WORKFLOW_ID,
        )

        wf = build_enrichment_workflow_test_definition()
        assert wf.workflow_id == ENRICHMENT_WORKFLOW_ID
        assert wf.initial_state == "collect"
        assert "collect" in wf.states
        assert "batch" in wf.states
        assert "generate" in wf.states
        assert "complete" in wf.states
        assert "failed" in wf.states

    def test_terminal_states(self) -> None:
        from src.backend.workflows.durable.enrichment_workflow import (
            build_enrichment_workflow_test_definition,
        )

        wf = build_enrichment_workflow_test_definition()
        assert wf.states["complete"].terminal is True
        assert wf.states["failed"].terminal is True
        assert wf.states["collect"].terminal is False

    def test_transitions_from_collect(self) -> None:
        from src.backend.workflows.durable.enrichment_workflow import (
            build_enrichment_workflow_test_definition,
        )

        wf = build_enrichment_workflow_test_definition()
        transitions = wf.states["collect"].transitions
        to_states = [t.to_state for t in transitions]
        assert "batch" in to_states
        assert "complete" in to_states


class TestEnrichmentActionHandlers:
    """Test individual action handlers."""

    def test_collect_requires_predicate(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.enrichment_workflow import (
            _handle_collect_candidates,
        )

        env = WorkflowEnvironment(llm_client=None)
        req = WorkflowActionRequest(
            action_id="enrichment.collect_candidates",
            inputs={},
            environment=env,
            data={},  # No predicate
        )

        result = _handle_collect_candidates(req)
        assert result.status == "failed"
        assert isinstance(result.error, str)
        assert "predicate_required" in result.error

    def test_collect_with_force_concept_ids(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.enrichment_workflow import (
            _handle_collect_candidates,
        )

        env = WorkflowEnvironment(llm_client=None)
        req = WorkflowActionRequest(
            action_id="enrichment.collect_candidates",
            inputs={},
            environment=env,
            data={
                "predicate": "hasDescription",
                "force_concept_ids": ["#V#alpha", "#V#beta"],
            },
        )

        result = _handle_collect_candidates(req)
        assert result.ok
        assert result.outputs["candidate_ids"] == ["#V#alpha", "#V#beta"]
        assert result.outputs["total_candidates"] == 2

    def test_collect_with_single_force_concept_id(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.enrichment_workflow import (
            _handle_collect_candidates,
        )

        env = WorkflowEnvironment(llm_client=None)
        req = WorkflowActionRequest(
            action_id="enrichment.collect_candidates",
            inputs={},
            environment=env,
            data={
                "predicate": "hasDescription",
                "force_concept_id": "#V#alpha",
            },
        )

        result = _handle_collect_candidates(req)
        assert result.ok
        assert result.outputs["candidate_ids"] == ["#V#alpha"]
        assert result.outputs["total_candidates"] == 1

    def test_collect_normalises_scalar_force_concept_ids(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.enrichment_workflow import (
            _handle_collect_candidates,
        )

        env = WorkflowEnvironment(llm_client=None)
        req = WorkflowActionRequest(
            action_id="enrichment.collect_candidates",
            inputs={},
            environment=env,
            data={
                "predicate": "hasDescription",
                "force_concept_ids": " #V#alpha ",
                "force_concept_id": "#V#alpha",
            },
        )

        result = _handle_collect_candidates(req)
        assert result.ok
        assert result.outputs["candidate_ids"] == ["#V#alpha"]
        assert result.outputs["total_candidates"] == 1

    def test_collect_scans_db(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.enrichment_workflow import (
            _handle_collect_candidates,
        )

        with (
            patch(
                "src.backend.workflows.durable.enrichment_workflow."
                "_find_concepts_missing_predicate"
            ) as mock_find,
        ):
            mock_find.return_value = ["#V#c1", "#V#c2", "#V#c3"]

            env = WorkflowEnvironment(llm_client=None)
            req = WorkflowActionRequest(
                action_id="enrichment.collect_candidates",
                inputs={},
                environment=env,
                data={"predicate": "hasDescription", "limit": 10},
            )

            result = _handle_collect_candidates(req)
            assert result.ok
            assert len(result.outputs["candidate_ids"]) == 3
            mock_find.assert_called_once_with("hasDescription", 10, kind_filter=None)

    def test_prepare_batch_slices_correctly(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.enrichment_workflow import (
            _handle_prepare_batch,
        )

        candidates = [f"#V#c{i}" for i in range(12)]
        env = WorkflowEnvironment(llm_client=None)

        # First batch
        req = WorkflowActionRequest(
            action_id="enrichment.prepare_batch",
            inputs={},
            environment=env,
            data={
                "candidate_ids": candidates,
                "batch_index": 0,
                "batch_size": 5,
            },
        )
        result = _handle_prepare_batch(req)
        assert result.ok
        assert len(result.outputs["current_batch"]) == 5
        assert result.outputs["has_more_batches"] is True
        assert result.outputs["batch_index"] == 1

        # Last batch
        req2 = WorkflowActionRequest(
            action_id="enrichment.prepare_batch",
            inputs={},
            environment=env,
            data={
                "candidate_ids": candidates,
                "batch_index": 2,
                "batch_size": 5,
            },
        )
        result2 = _handle_prepare_batch(req2)
        assert result2.ok
        assert len(result2.outputs["current_batch"]) == 2
        assert result2.outputs["has_more_batches"] is False

    def test_process_batch_generates_and_stores(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.enrichment_workflow import (
            _handle_process_batch,
        )

        mock_llm = MagicMock()
        mock_llm.generate.return_value = "A well-written description of the concept."

        with (
            patch(
                "src.backend.languagemodels.llm_interface.get_llm_client",
                return_value=mock_llm,
            ),
            patch(
                "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one"
            ) as mock_find,
            patch(
                "src.backend.workflows.durable.enrichment_workflow."
                "_upsert_text_value_internal"
            ) as mock_upsert,
            patch(
                "src.backend.workflows.durable.enrichment_workflow."
                "_build_concept_context",
                return_value={
                    "concept_name": "Test",
                    "concept_id": "#V#test",
                    "type_hierarchy": "is a type of: #V#thing",
                    "relationships": "none",
                    "description": "",
                },
            ),
        ):
            mock_find.return_value = {"concept_id": "#V#test"}

            env = WorkflowEnvironment(llm_client=mock_llm)
            req = WorkflowActionRequest(
                action_id="enrichment.process_batch",
                inputs={},
                environment=env,
                data={
                    "predicate": "hasDescription",
                    "current_batch": ["#V#test"],
                    "prompt_template": "Describe {concept_name} for retrieval.",
                },
            )

            result = _handle_process_batch(req)
            assert result.ok
            assert result.outputs["processed_count"] == 1
            assert result.outputs["failed_count"] == 0

            mock_upsert.assert_called_once_with(
                "#V#test",
                "hasDescription",
                "A well-written description of the concept.",
            )

    def test_process_batch_skips_missing_concept(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.enrichment_workflow import (
            _handle_process_batch,
        )

        mock_llm = MagicMock()

        with (
            patch(
                "src.backend.languagemodels.llm_interface.get_llm_client",
                return_value=mock_llm,
            ),
            patch(
                "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
                return_value=None,
            ),
        ):
            env = WorkflowEnvironment(llm_client=mock_llm)
            req = WorkflowActionRequest(
                action_id="enrichment.process_batch",
                inputs={},
                environment=env,
                data={
                    "predicate": "hasDescription",
                    "current_batch": ["#V#nonexistent"],
                },
            )

            result = _handle_process_batch(req)
            assert result.ok
            assert result.outputs["skipped_count"] == 1

    def test_process_batch_uses_deterministic_workflow_description_fallback(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.enrichment_workflow import (
            _handle_process_batch,
        )

        mock_llm = MagicMock()
        mock_llm.generate.side_effect = RuntimeError("llm unavailable")

        with (
            patch(
                "src.backend.languagemodels.llm_interface.get_llm_client",
                return_value=mock_llm,
            ),
            patch(
                "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one"
            ) as mock_find,
            patch(
                "src.backend.workflows.durable.enrichment_workflow."
                "_upsert_text_value_internal"
            ) as mock_upsert,
            patch(
                "src.backend.workflows.durable.enrichment_workflow."
                "_build_concept_context",
                return_value={
                    "concept_name": "Planning Workflow",
                    "concept_id": "#V#planning_workflow",
                    "type_hierarchy": "is an instance of: #V#durable_workflow",
                    "relationships": "invokesAction: planning.collect_context",
                    "description": "Forward inference workflow that proposes concrete next actions.",
                    "workflow_step_count": "6",
                    "workflow_action_ids": "planning.collect_context, planning.rank_actions",
                    "workflow_subworkflow_ids": "#V#tool_calling_workflow",
                    "workflow_transition_reasons": "context_ready, actions_ranked",
                    "workflow_output_context_keys": "planned_actions, rationale",
                    "workflow_graph_warnings": "None",
                },
            ),
        ):
            mock_find.return_value = {"concept_id": "#V#planning_workflow"}

            env = WorkflowEnvironment(llm_client=mock_llm)
            req = WorkflowActionRequest(
                action_id="enrichment.process_batch",
                inputs={},
                environment=env,
                data={
                    "predicate": "hasDescription",
                    "current_batch": ["#V#planning_workflow"],
                    "prompt_template": "Describe {concept_name} for retrieval.",
                },
            )

            result = _handle_process_batch(req)
            assert result.ok
            assert result.outputs["processed_count"] == 1
            assert result.outputs["failed_count"] == 0
            assert result.outputs["deterministic_fallback_count"] == 1

            stored_text = mock_upsert.call_args.args[2]
            assert "Domain:" in stored_text
            assert "Input types:" in stored_text
            assert "Estimated success likelihood:" in stored_text

    def test_process_batch_can_prefer_deterministic_workflow_description_mode(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.enrichment_workflow import (
            _handle_process_batch,
        )

        mock_llm = MagicMock()

        with (
            patch(
                "src.backend.languagemodels.llm_interface.get_llm_client",
                return_value=mock_llm,
            ),
            patch(
                "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
                return_value={"concept_id": "#V#planning_workflow"},
            ),
            patch(
                "src.backend.workflows.durable.enrichment_workflow."
                "_upsert_text_value_internal"
            ) as mock_upsert,
            patch(
                "src.backend.workflows.durable.enrichment_workflow."
                "_build_concept_context",
                return_value={
                    "concept_name": "Planning Workflow",
                    "concept_id": "#V#planning_workflow",
                    "type_hierarchy": "is an instance of: #V#durable_workflow",
                    "relationships": "invokesAction: planning.collect_context",
                    "description": "Forward inference workflow that proposes concrete next actions.",
                    "workflow_step_count": "6",
                    "workflow_action_ids": "planning.collect_context, planning.rank_actions",
                    "workflow_subworkflow_ids": "#V#tool_calling_workflow",
                    "workflow_transition_reasons": "context_ready, actions_ranked",
                    "workflow_output_context_keys": "planned_actions, rationale",
                    "workflow_graph_warnings": "None",
                },
            ),
        ):
            env = WorkflowEnvironment(llm_client=mock_llm)
            req = WorkflowActionRequest(
                action_id="enrichment.process_batch",
                inputs={},
                environment=env,
                data={
                    "predicate": "hasDescription",
                    "current_batch": ["#V#planning_workflow"],
                    "prefer_deterministic_workflow_description": True,
                    "prompt_template": "Describe {concept_name} for retrieval.",
                },
            )

            result = _handle_process_batch(req)
            assert result.ok
            assert result.outputs["processed_count"] == 1
            assert result.outputs["deterministic_fallback_count"] == 1
            mock_llm.generate.assert_not_called()
            stored_text = mock_upsert.call_args.args[2]
            assert stored_text.startswith(
                "Forward inference workflow that proposes concrete next actions."
            )

    def test_process_batch_fails_closed_when_prompt_is_unavailable(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.enrichment_workflow import (
            _handle_process_batch,
        )

        mock_llm = MagicMock()

        with (
            patch(
                "src.backend.languagemodels.llm_interface.get_llm_client",
                return_value=mock_llm,
            ),
            patch(
                "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
                return_value={"concept_id": "#V#planning_workflow"},
            ),
            patch(
                "src.backend.workflows.durable.enrichment_workflow._build_concept_context",
                return_value={"concept_name": "Planning Workflow"},
            ),
            patch(
                "src.backend.workflows.durable.enrichment_workflow._resolve_prompt_template",
                return_value=(
                    None,
                    {
                        "source": "none",
                        "prompt_concept_id": "#V#generate_concept_description_prompt",
                        "available": False,
                        "error": "enrichment_prompt_unavailable",
                    },
                ),
            ),
        ):
            req = WorkflowActionRequest(
                action_id="enrichment.process_batch",
                inputs={},
                environment=WorkflowEnvironment(llm_client=mock_llm),
                data={
                    "predicate": "hasDescription",
                    "current_batch": ["#V#planning_workflow"],
                },
            )

            result = _handle_process_batch(req)

        assert result.ok
        assert result.outputs["processed_count"] == 0
        assert result.outputs["failed_count"] == 1
        assert result.outputs["prompt_resolution_errors"] == [
            {
                "concept_id": "#V#planning_workflow",
                "predicate": "hasDescription",
                "diagnostics": {
                    "source": "none",
                    "prompt_concept_id": "#V#generate_concept_description_prompt",
                    "available": False,
                    "error": "enrichment_prompt_unavailable",
                },
            }
        ]

    def test_finalise_produces_summary(self) -> None:
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.enrichment_workflow import (
            _handle_finalise,
        )

        env = WorkflowEnvironment(llm_client=None)
        req = WorkflowActionRequest(
            action_id="enrichment.finalise",
            inputs={},
            environment=env,
            data={
                "predicate": "hasDescription",
                "total_candidates": 10,
                "processed_count": 8,
                "failed_count": 1,
                "skipped_count": 1,
            },
        )

        result = _handle_finalise(req)
        assert result.ok
        summary = result.outputs["enrichment_result"]
        assert summary["predicate"] == "hasDescription"
        assert summary["processed"] == 8
        assert summary["failed"] == 1
        assert summary["skipped"] == 1
        assert summary["success"] is False  # failed > 0


class TestEnrichmentRegistration:
    """Test that the workflow and actions register correctly."""

    def test_workflow_registration(self) -> None:
        from src.backend.workflows.durable.enrichment_workflow import (
            build_enrichment_workflow_test_registration,
            ENRICHMENT_WORKFLOW_ID,
        )

        reg = build_enrichment_workflow_test_registration()
        assert reg.workflow_id == ENRICHMENT_WORKFLOW_ID
        assert reg.source == "built_in"

    def test_action_registration(self) -> None:
        from src.backend.workflows.action_registry import ActionRegistry
        from src.backend.workflows.durable.enrichment_workflow import (
            register_enrichment_actions,
        )

        registry = ActionRegistry()
        register_enrichment_actions(registry)

        expected_actions = [
            "enrichment.collect_candidates",
            "enrichment.prepare_batch",
            "enrichment.process_batch",
            "enrichment.finalise",
        ]
        for action_id in expected_actions:
            assert registry.has(action_id), f"Missing action: {action_id}"

    def test_registered_in_factory(self) -> None:
        """Verify enrichment workflow appears in the unified registry factory."""
        from src.backend.workflows.durable.enrichment_workflow import (
            ENRICHMENT_WORKFLOW_ID,
        )
        from workflow_test_support import (
            bootstrap_authoritative_support_maintenance_workflows,
        )

        bootstrap_authoritative_support_maintenance_workflows()

        with patch(
            "src.backend.workflows.durable.registry_factory.discover_workflow_ids",
            return_value=[ENRICHMENT_WORKFLOW_ID],
        ):
            from src.backend.workflows.durable.registry_factory import (
                _build_workflow_registry,
            )

            registry = _build_workflow_registry(allow_bootstrap=False)
            assert ENRICHMENT_WORKFLOW_ID in registry.all_workflow_ids()
