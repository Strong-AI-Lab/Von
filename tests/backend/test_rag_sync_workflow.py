"""Tests for RAG sync durable workflow (JVNAUTOSCI-1075 Phase 4).

Validates the RAG text relation sync workflow definition, action handlers,
and the durable submission path in the sync service.
"""

from __future__ import annotations

import os
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_mock_db(monkeypatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    """Reset mock database before each test by clearing workflow collections."""
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


class TestRagSyncWorkflowDefinition:
    """Tests for the RAG sync workflow definition."""

    def test_workflow_id_is_correct(self) -> None:
        """Workflow ID should follow Vontology naming convention."""
        from src.backend.workflows.durable.rag_sync_workflow import (
            RAG_TEXT_RELATION_SYNC_WORKFLOW_ID,
        )

        assert (
            RAG_TEXT_RELATION_SYNC_WORKFLOW_ID == "#V#rag_text_relation_sync_workflow"
        )

    def test_build_workflow_returns_definition(self) -> None:
        """build_rag_text_relation_sync_workflow_test_definition() should return a WorkflowDefinition."""
        from src.backend.workflows.durable.rag_sync_workflow import (
            build_rag_text_relation_sync_workflow_test_definition,
        )
        from src.backend.workflows.engine import WorkflowDefinition

        workflow = build_rag_text_relation_sync_workflow_test_definition()

        assert isinstance(workflow, WorkflowDefinition)
        assert workflow.workflow_id == "#V#rag_text_relation_sync_workflow"

    def test_workflow_has_expected_states(self) -> None:
        """Workflow should have correct state machine structure."""
        from src.backend.workflows.durable.rag_sync_workflow import (
            build_rag_text_relation_sync_workflow_test_definition,
        )

        workflow = build_rag_text_relation_sync_workflow_test_definition()
        # states is a dict[str, WorkflowStateSpec]
        state_ids = list(workflow.states.keys())

        assert "collect" in state_ids
        assert "batch" in state_ids
        assert "upsert" in state_ids
        assert "complete" in state_ids
        assert "failed" in state_ids

    def test_workflow_initial_state_is_collect(self) -> None:
        """Workflow should start at the collect state."""
        from src.backend.workflows.durable.rag_sync_workflow import (
            build_rag_text_relation_sync_workflow_test_definition,
        )

        workflow = build_rag_text_relation_sync_workflow_test_definition()

        assert workflow.initial_state == "collect"

    def test_workflow_terminal_states(self) -> None:
        """Complete and failed should be terminal states."""
        from src.backend.workflows.durable.rag_sync_workflow import (
            build_rag_text_relation_sync_workflow_test_definition,
        )

        workflow = build_rag_text_relation_sync_workflow_test_definition()

        assert "complete" in workflow.termination_states
        assert "failed" in workflow.termination_states


class TestRagSyncActionHandlers:
    """Tests for the RAG sync action handlers."""

    def test_register_actions_adds_all_handlers(self) -> None:
        """register_rag_sync_actions() should register 4 action handlers."""
        from src.backend.workflows.action_registry import ActionRegistry
        from src.backend.workflows.durable.rag_sync_workflow import (
            register_rag_sync_actions,
        )

        registry = ActionRegistry()
        register_rag_sync_actions(registry)

        assert registry.get("rag_sync.collect_docs") is not None
        assert registry.get("rag_sync.prepare_batch") is not None
        assert registry.get("rag_sync.upsert_batch") is not None
        assert registry.get("rag_sync.finalise") is not None

    def test_collect_docs_handler_calls_service(self) -> None:
        """Collect docs handler should call the sync service."""
        from src.backend.workflows.action_registry import (
            ActionRegistry,
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.rag_sync_workflow import (
            register_rag_sync_actions,
        )

        registry = ActionRegistry()
        register_rag_sync_actions(registry)

        spec = registry.get("rag_sync.collect_docs")
        assert spec is not None
        handler = spec.handler

        # Mock the service function - create mock docs with required attributes
        mock_doc1 = MagicMock()
        mock_doc1.doc_id = "doc1"
        mock_doc1.text = "text1"
        mock_doc1.metadata = {}

        mock_doc2 = MagicMock()
        mock_doc2.doc_id = "doc2"
        mock_doc2.text = "text2"
        mock_doc2.metadata = {}

        mock_docs = [mock_doc1, mock_doc2]

        # Patch where the import actually loads (the service module)
        with patch(
            "src.backend.services.rag_text_relation_sync_service.collect_text_relation_docs_for_namespace",
            return_value=mock_docs,
        ):
            env = WorkflowEnvironment(llm_client=None)
            request = WorkflowActionRequest(
                action_id="rag_sync.collect_docs",
                inputs={},
                environment=env,
                data={"namespace": "#V#test", "limit": 100},
            )

            result = handler(request)

        assert result.status == "success"
        assert result.outputs["total_candidates"] == 2

    def test_prepare_batch_handler_chunks_data(self) -> None:
        """Prepare batch handler should chunk document list."""
        from src.backend.workflows.action_registry import (
            ActionRegistry,
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.rag_sync_workflow import (
            register_rag_sync_actions,
        )

        registry = ActionRegistry()
        register_rag_sync_actions(registry)

        spec = registry.get("rag_sync.prepare_batch")
        assert spec is not None
        handler = spec.handler

        env = WorkflowEnvironment(llm_client=None)
        request = WorkflowActionRequest(
            action_id="rag_sync.prepare_batch",
            inputs={},
            environment=env,
            data={
                "batch_size": 2,
                "collected_docs": [
                    {"doc_id": "d1", "text": "t1", "metadata": {}},
                    {"doc_id": "d2", "text": "t2", "metadata": {}},
                    {"doc_id": "d3", "text": "t3", "metadata": {}},
                ],
                "batch_index": 0,
            },
        )

        result = handler(request)

        assert result.status == "success"
        assert len(result.outputs["current_batch"]) == 2  # First batch of 2
        assert result.outputs["has_more_batches"] is True

    def test_finalise_handler_produces_summary(self) -> None:
        """Finalise handler should produce sync summary."""
        from src.backend.workflows.action_registry import (
            ActionRegistry,
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.rag_sync_workflow import (
            register_rag_sync_actions,
        )

        registry = ActionRegistry()
        register_rag_sync_actions(registry)

        spec = registry.get("rag_sync.finalise")
        assert spec is not None
        handler = spec.handler

        env = WorkflowEnvironment(llm_client=None)
        request = WorkflowActionRequest(
            action_id="rag_sync.finalise",
            inputs={},
            environment=env,
            data={
                "namespace": "#V#test",
                "total_candidates": 10,
                "added": 8,
                "failed": 2,
            },
        )

        result = handler(request)

        assert result.status == "success"
        assert result.outputs["sync_result"]["namespace"] == "#V#test"
        assert result.outputs["sync_result"]["total_candidates"] == 10
        assert result.outputs["sync_result"]["added"] == 8
        assert result.outputs["sync_result"]["failed"] == 2


class TestSubmitDurableRagSync:
    """Tests for the durable submission path."""

    def test_submit_fails_when_disabled(self) -> None:
        """submit_durable_rag_sync() should fail when workflows are disabled."""
        from src.backend.services.rag_text_relation_sync_service import (
            submit_durable_rag_sync,
        )

        # Ensure disabled
        with patch.dict(os.environ, {"VON_DURABLE_WORKFLOWS_ENABLE": "0"}):
            result = submit_durable_rag_sync(
                namespace="#V#test",
                user_id="user-1",
                org_id="org-1",
            )

        assert result["success"] is False
        assert result["error"] == "durable_workflows_disabled"

    def test_submit_succeeds_when_enabled(self) -> None:
        """submit_durable_rag_sync() should create workflow instance when enabled."""
        from src.backend.workflows.durable.instance_manager import (
            WorkflowInstanceManager,
        )
        from src.backend.workflows.durable.workflow_instance_submission_service import (
            WorkflowInstanceSubmissionResult,
        )

        # Enable workflows and mock the startup module
        with patch.dict(os.environ, {"VON_DURABLE_WORKFLOWS_ENABLE": "1"}):
            # Mock the instance manager
            mock_manager = MagicMock(spec=WorkflowInstanceManager)

            with patch(
                "src.backend.workflows.durable.startup.get_instance_manager",
                return_value=mock_manager,
            ), patch(
                "src.backend.workflows.durable.workflow_instance_submission_service.submit_verified_workflow_instance",
                return_value=WorkflowInstanceSubmissionResult(
                    success=True,
                    workflow_id="#V#rag_text_relation_sync_workflow",
                    status="pending",
                    instance_id="test-instance-id",
                    verification={
                        "preflight_passed": True,
                        "postflight_passed": True,
                        "runnable_verification_success": True,
                    },
                    created_new=True,
                ),
            ) as mock_submit:
                # Import after patching
                from src.backend.services.rag_text_relation_sync_service import (
                    submit_durable_rag_sync,
                )

                result = submit_durable_rag_sync(
                    namespace="#V#test",
                    user_id="user-1",
                    org_id="org-1",
                    predicates=["#V#hasContent"],
                    limit=1000,
                    batch_size=100,
                )

        assert result["success"] is True
        assert result["instance_id"] == "test-instance-id"
        assert result["namespace"] == "#V#test"
        assert result["created_new"] is True
        assert result["submission_status"] == "pending"
        assert result["verification"]["runnable_verification_success"] is True

        mock_submit.assert_called_once()


class TestWorkflowRegistration:
    """Tests for workflow registration in startup."""

    def test_build_rag_text_relation_sync_workflow_test_registration_returns_registration(self) -> None:
        """build_rag_text_relation_sync_workflow_test_registration() should return a WorkflowRegistration."""
        from src.backend.workflows.durable.rag_sync_workflow import (
            build_rag_text_relation_sync_workflow_test_registration,
            RAG_TEXT_RELATION_SYNC_WORKFLOW_ID,
        )
        from src.backend.workflows.workflow_registry import WorkflowRegistration

        registration = build_rag_text_relation_sync_workflow_test_registration()

        assert isinstance(registration, WorkflowRegistration)
        assert registration.workflow_id == RAG_TEXT_RELATION_SYNC_WORKFLOW_ID
        assert registration.definition is not None
