"""Tests for Considerations for Use workflow (JVNAUTOSCI-1081)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, ANY

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


class TestConsiderationsWorkflowDefinition:

    def test_workflow_structure(self) -> None:
        from src.backend.workflows.durable.considerations_workflow import (
            build_generate_considerations_workflow,
            GENERATE_CONSIDERATIONS_WORKFLOW_ID,
        )

        wf = build_generate_considerations_workflow()
        assert wf.workflow_id == GENERATE_CONSIDERATIONS_WORKFLOW_ID
        assert "collect" in wf.states
        assert "batch" in wf.states
        assert "generate" in wf.states
        assert "complete" in wf.states
        assert wf.initial_state == "collect"


class TestConsiderationsActionHandlers:

    def test_collect_candidates(self) -> None:
        from src.backend.workflows.action_registry import (
            ActionRegistry,
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.considerations_workflow import (
            register_considerations_actions,
        )

        registry = ActionRegistry()
        register_considerations_actions(registry)
        handler = registry.get("considerations.collect_candidates").handler

        # Mock dependencies
        with (
            patch(
                "src.backend.db.repositories.text_value_repository.TextRelationsRepository.find"
            ) as mock_find_rels,
            patch(
                "src.backend.db.repositories.concepts_repository.ConceptsRepository.find"
            ) as mock_find_concepts,
        ):

            # Setup mock returns
            mock_find_rels.return_value = []  # No excluded
            mock_find_concepts.return_value = [
                {"concept_id": "c1"},
                {"concept_id": "c2"},
            ]

            env = WorkflowEnvironment(llm_client=None)
            req = WorkflowActionRequest(
                action_id="considerations.collect_candidates",
                inputs={},
                environment=env,
                data={},
            )

            result = handler(req)

            assert result.status == "success"
            assert result.outputs["candidate_ids"] == ["c1", "c2"]

    @pytest.mark.asyncio
    async def test_process_batch_generates_and_upserts(self) -> None:
        from src.backend.workflows.action_registry import (
            ActionRegistry,
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.considerations_workflow import (
            _handle_process_batch,
            PREDICATE_ID,
        )

        # Mock LLM and repos
        mock_llm = MagicMock()
        mock_llm.generate.return_value.text = "Generated Considerations"

        with (
            patch(
                "src.backend.workflows.durable.considerations_workflow.get_llm_client",
                return_value=mock_llm,
            ),
            patch(
                "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one"
            ) as mock_find_concept,
            patch(
                "src.backend.workflows.durable.considerations_workflow._upsert_text_value_internal"
            ) as mock_upsert,
            patch(
                "src.backend.workflows.durable.considerations_workflow.get_concept_display_name_with_names_fallback",
                return_value="Test Concept",
            ),
            patch(
                "src.backend.workflows.durable.considerations_workflow.get_concept_description",
                return_value="Desc",
            ),
        ):

            mock_find_concept.return_value = {"concept_id": "c1"}

            env = WorkflowEnvironment(llm_client=mock_llm)
            req = WorkflowActionRequest(
                action_id="considerations.process_batch",
                inputs={},
                environment=env,
                data={"current_batch": ["c1"]},
            )

            # Handler is async
            if asyncio_iscoroutinefunction(_handle_process_batch):
                result = await _handle_process_batch(req)
            else:
                result = _handle_process_batch(req)

            assert result.status == "success"
            assert result.outputs["processed_count"] == 1

            mock_upsert.assert_called_with(
                "c1", PREDICATE_ID, "Generated Considerations"
            )


def asyncio_iscoroutinefunction(func):
    import inspect

    return inspect.iscoroutinefunction(func)
