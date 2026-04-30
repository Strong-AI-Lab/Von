"""Tests for task management service (JVNAUTOSCI-1040).

Unit tests for task creation, retrieval, status updates, and assignment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.backend.services.task_management_service import (
    TASK_SPECIFICATION_TYPE_ID,
    TASK_STATUS_PENDING,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_CANCELLED,
    VALID_TASK_STATUSES,
    VALID_PRIORITIES,
    PRIORITY_MEDIUM,
    JIRA_MIGRATION_BULK_COLLECTION_ID,
    TaskNotFoundError,
    InvalidTaskDataError,
    apply_bulk_task_visibility,
    backfill_jira_migration_bulk_task_collections,
    build_jira_migration_bulk_task_collection,
    create_task,
    get_task,
    find_task_by_external_reference,
    add_task_attachment,
    add_task_comment,
    add_task_worklog,
    record_task_history_event,
    upsert_task_external_reference,
    update_task_status,
    update_task_fields,
    assign_task,
    get_tasks_for_user,
    list_tasks,
    list_tasks_with_visibility,
    search_tasks,
    delete_task,
)
from src.backend.services.task_ontology_service import (
    DEFAULT_TASK_SOURCE_ID,
    DEFAULT_TASK_TYPE_ID,
    JIRA_IMPORTED_TASK_SOURCE_ID,
    PREDICATE_HAS_TASK_REFERENCE_CODE,
    PREDICATE_HAS_TASK_ROLE,
    PREDICATE_HAS_TASK_SOURCE,
    PREDICATE_HAS_NEXT_CHECKPOINT,
    PREDICATE_HAS_PROGRESS_SIGNAL,
    PREDICATE_HAS_EVIDENCE,
    PREDICATE_REPORTS_TO,
)


class TestTaskConstants:
    """Test task service constants."""

    def test_valid_statuses(self) -> None:
        """VALID_TASK_STATUSES should contain expected values."""
        assert "pending" in VALID_TASK_STATUSES
        assert "in_progress" in VALID_TASK_STATUSES
        assert "completed" in VALID_TASK_STATUSES
        assert "cancelled" in VALID_TASK_STATUSES
        assert "blocked" in VALID_TASK_STATUSES

    def test_valid_priorities(self) -> None:
        """VALID_PRIORITIES should contain expected values."""
        assert "low" in VALID_PRIORITIES
        assert "medium" in VALID_PRIORITIES
        assert "high" in VALID_PRIORITIES
        assert "critical" in VALID_PRIORITIES

    def test_status_constants(self) -> None:
        """Status constants should match expected strings."""
        assert TASK_STATUS_PENDING == "pending"
        assert TASK_STATUS_IN_PROGRESS == "in_progress"
        assert TASK_STATUS_COMPLETED == "completed"
        assert TASK_STATUS_CANCELLED == "cancelled"


class TestCreateTask:
    """Tests for create_task function."""

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.upsert_text_for_concept")
    @patch(
        "src.backend.services.task_management_service.maybe_launch_task_created_workflow"
    )
    def test_create_task_with_minimal_params(
        self,
        mock_launch_workflow: MagicMock,
        mock_upsert: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        """create_task() with only required params should work."""
        mock_repo.insert_one.return_value = None

        result = create_task(
            title="Test Task",
            description="Test task description",
        )

        assert result["title"] == "Test Task"
        assert result["status"] == TASK_STATUS_PENDING
        assert result["priority"] == PRIORITY_MEDIUM
        assert "task_concept_id" in result
        assert result["task_concept_id"].startswith("#V#task_")
        assert result["task_type_ids"] == [DEFAULT_TASK_TYPE_ID]
        assert result["task_source_id"] == DEFAULT_TASK_SOURCE_ID
        mock_launch_workflow.assert_called_once()

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.upsert_text_for_concept")
    @patch(
        "src.backend.services.conversation_concept_service.get_or_create_conversation_concept"
    )
    @patch(
        "src.backend.services.task_management_service.maybe_launch_task_created_workflow"
    )
    def test_create_task_with_all_params(
        self,
        mock_launch_workflow: MagicMock,
        mock_conv_service: MagicMock,
        mock_upsert: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        """create_task() with all params should store correctly."""
        mock_repo.insert_one.return_value = None
        mock_conv_service.return_value = "#V#conversation_abc123"

        result = create_task(
            title="Full Task",
            description="Complete description",
            assignee_concept_id="#V#user_alice",
            created_by_concept_id="#V#user_bob",
            originating_session_id="session-123",
            due_date=datetime(2025, 2, 1, 12, 0, 0, tzinfo=timezone.utc),
            priority="high",
            organisation_concept_id="#V#nao_institute",
            task_type_ids=["#V#delegated_task_specification"],
            task_source_id=JIRA_IMPORTED_TASK_SOURCE_ID,
            report_to_concept_id="#V#user_carol",
            task_role="Communicator",
            next_checkpoint="Tomorrow morning",
            progress_signal="Confirmed by chat",
            evidence="Printed document",
            notes="Needs a coloured copy",
            reference_code="TASK-001",
        )

        assert result["title"] == "Full Task"
        assert result["status"] == TASK_STATUS_PENDING
        assert result["priority"] == "high"
        assert result["assignee_concept_id"] == "#V#user_alice"
        assert result["task_type_ids"] == ["#V#delegated_task_specification"]
        assert result["task_source_id"] == JIRA_IMPORTED_TASK_SOURCE_ID
        assert result["report_to_concept_id"] == "#V#user_carol"
        assert result["task_role"] == "Communicator"
        assert result["next_checkpoint"] == "Tomorrow morning"
        assert result["progress_signal"] == "Confirmed by chat"
        assert result["evidence"] == "Printed document"
        assert result["notes"] == "Needs a coloured copy"
        assert result["reference_code"] == "TASK-001"
        stored_predicates = {
            call.kwargs.get("predicate") for call in mock_upsert.call_args_list
        }
        assert PREDICATE_HAS_TASK_ROLE in stored_predicates
        assert PREDICATE_HAS_NEXT_CHECKPOINT in stored_predicates
        assert PREDICATE_HAS_PROGRESS_SIGNAL in stored_predicates
        assert PREDICATE_HAS_EVIDENCE in stored_predicates
        assert PREDICATE_HAS_TASK_REFERENCE_CODE in stored_predicates
        mock_launch_workflow.assert_called_once()

    def test_create_task_invalid_title(self) -> None:
        """create_task() with empty title should raise InvalidTaskDataError."""
        with pytest.raises(InvalidTaskDataError, match="title is required"):
            create_task(title="", description="Some description")

    def test_create_task_invalid_description(self) -> None:
        """create_task() with empty description should raise InvalidTaskDataError."""
        with pytest.raises(InvalidTaskDataError, match="description is required"):
            create_task(title="Valid Title", description="")

    def test_create_task_invalid_priority(self) -> None:
        """create_task() with invalid priority should raise InvalidTaskDataError."""
        with pytest.raises(InvalidTaskDataError, match="Invalid priority"):
            create_task(
                title="Test",
                description="Test desc",
                priority="invalid_priority",
            )


class TestGetTask:
    """Tests for get_task function."""

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    def test_get_task_found(
        self,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        """get_task() should return task details when found."""
        mock_repo.find_one.return_value = {
            "concept_id": "#V#task_abc123",
            "relationships": {
                "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID],
                "#V#hasAssignee": ["#V#user_alice"],
            },
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        mock_get_texts.return_value = [
            {"predicate": "#V#hasName", "text": "My Task"},
            {"predicate": "#V#hasDescription", "text": "Task description"},
            {"predicate": "#V#hasTaskStatus", "text": "pending"},
            {"predicate": "#V#hasPriority", "text": "high"},
        ]

        result = get_task("#V#task_abc123")

        assert result["task_concept_id"] == "#V#task_abc123"
        assert result["title"] == "My Task"
        assert result["description"] == "Task description"
        assert result["status"] == "pending"
        assert result["priority"] == "high"
        assert result["assignee_concept_id"] == "#V#user_alice"

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    def test_get_task_supports_legacy_task_taxonomy_predicates(
        self,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        now = datetime.now(timezone.utc)
        mock_repo.find_one.return_value = {
            "concept_id": "#V#task_legacy",
            "relationships": {
                "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID],
                "#V#hasTaskSource": [JIRA_IMPORTED_TASK_SOURCE_ID],
                "#V#reportsTo": ["#V#user_manager"],
            },
            "metadata": {},
            "created_at": now,
            "updated_at": now,
        }
        mock_get_texts.return_value = [
            {"predicate": "#V#hasName", "text": "Legacy Task"},
            {"predicate": "#V#hasDescription", "text": "Legacy description"},
            {"predicate": "#V#hasTaskStatus", "text": "pending"},
            {"predicate": "#V#hasTaskRole", "text": "Communicator"},
            {"predicate": "#V#hasNextCheckpoint", "text": "Tomorrow morning"},
            {"predicate": "#V#hasProgressSignal", "text": "Confirmed by chat"},
            {"predicate": "#V#hasEvidence", "text": "Printed document"},
            {"predicate": "#V#hasTaskReferenceCode", "text": "TASK-001"},
        ]

        result = get_task("#V#task_legacy")

        assert result["task_source_id"] == JIRA_IMPORTED_TASK_SOURCE_ID
        assert result["report_to_concept_id"] == "#V#user_manager"
        assert result["task_role"] == "Communicator"
        assert result["next_checkpoint"] == "Tomorrow morning"
        assert result["progress_signal"] == "Confirmed by chat"
        assert result["evidence"] == "Printed document"
        assert result["reference_code"] == "TASK-001"

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    def test_get_task_not_found(self, mock_repo: MagicMock) -> None:
        """get_task() should raise TaskNotFoundError when not found."""
        mock_repo.find_one.return_value = None

        with pytest.raises(TaskNotFoundError, match="not found"):
            get_task("#V#task_nonexistent")

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    def test_get_task_not_a_task(self, mock_repo: MagicMock) -> None:
        """get_task() should raise TaskNotFoundError if concept is not a task."""
        mock_repo.find_one.return_value = {
            "concept_id": "#V#not_a_task",
            "relationships": {
                "is_an_instance_of": ["#V#person"],  # Not a task
            },
        }

        with pytest.raises(TaskNotFoundError, match="not a task"):
            get_task("#V#not_a_task")


class TestUpdateTaskStatus:
    """Tests for update_task_status function."""

    @patch("src.backend.services.task_management_service.get_task")
    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    @patch("src.backend.services.task_management_service.upsert_text_for_concept")
    @patch(
        "src.backend.services.task_management_service.maybe_launch_task_status_workflow"
    )
    def test_update_status_valid(
        self,
        mock_launch_workflow: MagicMock,
        mock_upsert: MagicMock,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
        mock_get_task: MagicMock,
    ) -> None:
        """update_task_status() with valid status should update."""
        mock_repo.find_one.return_value = {
            "concept_id": "#V#task_abc",
            "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
            "metadata": {},
        }
        mock_get_texts.return_value = [
            {"predicate": "#V#hasTaskStatus", "text": "pending"},
        ]
        mock_get_task.return_value = {
            "task_concept_id": "#V#task_abc",
            "status": "in_progress",
            "updated_at": datetime.now(timezone.utc),
        }

        result = update_task_status("#V#task_abc", "in_progress")

        assert result["status"] == "in_progress"
        mock_upsert.assert_called()
        mock_launch_workflow.assert_called_once()

    @patch("src.backend.services.task_management_service.get_task")
    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    @patch("src.backend.services.task_management_service.upsert_text_for_concept")
    @patch("src.backend.services.task_management_service.ensure_effort_unit_ontology")
    @patch(
        "src.backend.services.task_management_service.maybe_launch_effort_unit_completed_workflow"
    )
    @patch(
        "src.backend.services.task_management_service.persist_successor_effort_unit_type_links"
    )
    @patch(
        "src.backend.services.task_management_service.resolve_successor_effort_unit_type_ids"
    )
    @patch(
        "src.backend.services.task_management_service.maybe_launch_task_status_workflow"
    )
    def test_update_status_completed_persists_successor_linkage(
        self,
        mock_launch_status_workflow: MagicMock,
        mock_resolve_successors: MagicMock,
        mock_persist_successors: MagicMock,
        mock_launch_completion_workflow: MagicMock,
        mock_ensure_effort_unit: MagicMock,
        mock_upsert: MagicMock,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
        mock_get_task: MagicMock,
    ) -> None:
        mock_repo.find_one.return_value = {
            "concept_id": "#V#task_abc",
            "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
            "metadata": {},
        }
        mock_get_texts.return_value = [
            {"predicate": "#V#hasTaskStatus", "text": "in_progress"},
        ]
        mock_get_task.return_value = {
            "task_concept_id": "#V#task_abc",
            "status": "completed",
            "updated_at": datetime.now(timezone.utc),
            "created_by_concept_id": "#V#user_alice",
            "organisation_concept_id": "#V#org_nao",
        }
        mock_resolve_successors.return_value = ["#V#conference_presentation"]
        mock_persist_successors.return_value = {
            "success": True,
            "linked_type_ids": ["#V#conference_presentation"],
            "already_linked_type_ids": [],
            "skipped_missing_target_type_ids": [],
            "errors": [],
        }
        mock_launch_completion_workflow.return_value = {
            "success": True,
            "triggered": True,
        }
        mock_launch_status_workflow.return_value = {"success": True, "triggered": True}

        result = update_task_status("#V#task_abc", "completed")

        assert result["status"] == "completed"
        assert result["successor_effort_unit_linkage"]["linked_type_ids"] == [
            "#V#conference_presentation"
        ]
        mock_ensure_effort_unit.assert_called_once()
        mock_resolve_successors.assert_called_once()
        mock_persist_successors.assert_called_once()
        mock_launch_completion_workflow.assert_called_once()
        mock_launch_status_workflow.assert_called_once()

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    @patch("src.backend.services.task_management_service.upsert_text_for_concept")
    @patch(
        "src.backend.services.task_management_service.maybe_launch_task_status_workflow"
    )
    def test_update_status_unchanged_skips_write_and_event(
        self,
        mock_launch_workflow: MagicMock,
        mock_upsert: MagicMock,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        mock_repo.find_one.return_value = {
            "concept_id": "#V#task_abc",
            "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
            "metadata": {},
        }
        mock_get_texts.return_value = [
            {"predicate": "#V#hasTaskStatus", "text": "in_progress"},
        ]

        result = update_task_status("#V#task_abc", "in_progress")

        assert result["status"] == "in_progress"
        mock_upsert.assert_not_called()
        mock_launch_workflow.assert_not_called()

    def test_update_status_invalid(self) -> None:
        """update_task_status() with invalid status should raise error."""
        with pytest.raises(InvalidTaskDataError, match="Invalid status"):
            update_task_status("#V#task_abc", "invalid_status")


class TestAssignTask:
    """Tests for assign_task function."""

    @patch("src.backend.services.task_management_service.get_task")
    @patch("src.backend.services.task_management_service.ConceptsRepository")
    def test_assign_task_success(
        self,
        mock_repo: MagicMock,
        mock_get_task: MagicMock,
    ) -> None:
        """assign_task() should update assignee relationship."""
        mock_repo.find_one.return_value = {
            "concept_id": "#V#task_abc",
            "relationships": {
                "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID],
                "#V#hasAssignee": [],
            },
        }
        mock_get_task.return_value = {
            "task_concept_id": "#V#task_abc",
            "assignee_concept_id": "#V#user_new",
        }

        result = assign_task("#V#task_abc", "#V#user_new")

        assert result["assignee_concept_id"] == "#V#user_new"
        # Verify mutate_relationship_edge was called for add
        mock_repo.mutate_relationship_edge.assert_called()


class TestGetTasksForUser:
    """Tests for get_tasks_for_user function."""

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    def test_get_tasks_for_user_assigned(
        self,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        """get_tasks_for_user() should return tasks assigned to user."""
        now = datetime.now(timezone.utc)
        mock_repo.find.return_value = [
            {
                "concept_id": "#V#task_1",
                "relationships": {
                    "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID],
                    "#V#hasAssignee": ["#V#user_alice"],
                },
                "created_at": now,
                "updated_at": now,
            }
        ]
        mock_get_texts.return_value = [
            {"predicate": "#V#hasName", "text": "Task 1"},
            {"predicate": "#V#hasTaskStatus", "text": "pending"},
        ]

        result = get_tasks_for_user("#V#user_alice")

        assert len(result) == 1
        assert result[0]["task_concept_id"] == "#V#task_1"

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    def test_get_tasks_for_user_empty(self, mock_repo: MagicMock) -> None:
        """get_tasks_for_user() should return empty list if no tasks."""
        mock_repo.find.return_value = []

        result = get_tasks_for_user("#V#user_no_tasks")

        assert result == []


class TestListTasks:
    """Tests for list_tasks function."""

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    def test_list_tasks_all(
        self,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        """list_tasks() without filters should return all tasks."""
        now = datetime.now(timezone.utc)
        mock_repo.find.return_value = [
            {
                "concept_id": "#V#task_1",
                "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
                "created_at": now,
                "updated_at": now,
            },
            {
                "concept_id": "#V#task_2",
                "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
                "created_at": now,
                "updated_at": now,
            },
        ]
        mock_get_texts.return_value = [
            {"predicate": "#V#hasTaskStatus", "text": "pending"},
        ]

        result = list_tasks()

        assert len(result) == 2


class TestBulkTaskCollections:
    """Tests for hidden-by-default bulk task collection support."""

    def test_apply_bulk_task_visibility_hides_marked_tasks(self) -> None:
        collection = build_jira_migration_bulk_task_collection()
        result = apply_bulk_task_visibility(
            [
                {"task_concept_id": "#V#task_native", "title": "Native"},
                {
                    "task_concept_id": "#V#task_migrated",
                    "title": "Migrated",
                    "bulk_task_collections": [collection],
                },
            ],
            bulk_visibility="exclude",
        )

        assert [task["task_concept_id"] for task in result["tasks"]] == [
            "#V#task_native"
        ]
        assert result["hidden_bulk_task_total"] == 1
        assert result["hidden_bulk_task_collections"][0]["collection_id"] == (
            JIRA_MIGRATION_BULK_COLLECTION_ID
        )

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    def test_search_tasks_excludes_bulk_collection_before_pagination(
        self,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        now = datetime.now(timezone.utc)
        collection = build_jira_migration_bulk_task_collection()
        mock_repo.find.return_value = [
            {
                "concept_id": "#V#task_migrated",
                "relationships": {
                    "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID],
                    PREDICATE_HAS_TASK_SOURCE: [JIRA_IMPORTED_TASK_SOURCE_ID],
                },
                "metadata": {"bulk_task_collections": [collection]},
                "created_at": now,
                "updated_at": now,
            },
            {
                "concept_id": "#V#task_native",
                "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
                "metadata": {},
                "created_at": now,
                "updated_at": now,
            },
        ]
        mock_get_texts.return_value = [
            {"predicate": "#V#hasName", "text": "Task"},
            {"predicate": "#V#hasTaskStatus", "text": "pending"},
        ]

        result = search_tasks(bulk_visibility="exclude", limit=10)

        assert result["count"] == 1
        assert result["tasks"][0]["task_concept_id"] == "#V#task_native"
        assert result["hidden_bulk_task_total"] == 1
        assert result["hidden_bulk_task_collections"][0]["label"] == (
            "Jira migration backlog"
        )

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    def test_list_tasks_with_visibility_excludes_hidden_bulk_docs_in_query(
        self,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        now = datetime.now(timezone.utc)
        collection = build_jira_migration_bulk_task_collection()
        hidden_doc = {
            "concept_id": "#V#task_hidden_bulk",
            "relationships": {
                "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID],
                "#V#hasAssignee": ["#V#user_alice"],
            },
            "metadata": {"bulk_task_collections": [collection]},
            "created_at": now,
            "updated_at": now,
        }
        visible_doc = {
            "concept_id": "#V#task_visible",
            "relationships": {
                "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID],
                "#V#hasAssignee": ["#V#user_alice"],
            },
            "metadata": {},
            "created_at": now,
            "updated_at": now,
        }

        def _find_side_effect(*args, **kwargs):
            if kwargs.get("projection"):
                return [hidden_doc]
            return [visible_doc]

        mock_repo.find.side_effect = _find_side_effect
        mock_repo.count_documents.return_value = 1
        mock_get_texts.return_value = [
            {"predicate": "#V#hasName", "text": "Visible task"},
            {"predicate": "#V#hasTaskStatus", "text": "pending"},
        ]

        result = list_tasks_with_visibility(
            assignee_concept_id="#V#user_alice",
            bulk_visibility="exclude",
            limit=10,
        )

        assert result["count"] == 1
        assert result["tasks"][0]["task_concept_id"] == "#V#task_visible"
        assert result["hidden_bulk_task_total"] == 1
        assert result["hidden_bulk_task_collections"][0]["collection_id"] == (
            JIRA_MIGRATION_BULK_COLLECTION_ID
        )
        telemetry = result["load_telemetry"]
        assert telemetry["schema_version"] == "task_list_load_telemetry.v1"
        assert telemetry["source"] == "task_management.list_tasks_with_visibility"
        assert telemetry["request"]["bulk_visibility"] == "exclude"
        assert telemetry["request"]["requires_post_filter"] is False
        assert any(
            stage["stage"] == "repository_find_page"
            for stage in telemetry["stages"]
        )
        assert mock_get_texts.call_count == 1
        visible_query = mock_repo.find.call_args_list[1].args[0]
        assert "$nor" in visible_query["$and"][-1]
        assert {"relationships.#V#hasAssignee": "#V#user_alice"} in visible_query[
            "$and"
        ]

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    def test_backfill_jira_migration_bulk_task_collections_dry_run_marks_only_labelled_imports(
        self,
        mock_repo: MagicMock,
    ) -> None:
        now = datetime.now(timezone.utc)
        mock_repo.find.return_value = [
            {
                "concept_id": "#V#task_labelled_import",
                "relationships": {
                    "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID],
                    PREDICATE_HAS_TASK_SOURCE: [JIRA_IMPORTED_TASK_SOURCE_ID],
                },
                "metadata": {"labels": ["migrated"]},
                "created_at": now,
                "updated_at": now,
            },
            {
                "concept_id": "#V#task_unlabelled_import",
                "relationships": {
                    "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID],
                    PREDICATE_HAS_TASK_SOURCE: [JIRA_IMPORTED_TASK_SOURCE_ID],
                },
                "metadata": {"labels": []},
                "created_at": now,
                "updated_at": now,
            },
            {
                "concept_id": "#V#task_native",
                "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
                "metadata": {"labels": ["migrated"]},
                "created_at": now,
                "updated_at": now,
            },
        ]

        result = backfill_jira_migration_bulk_task_collections(dry_run=True)

        assert result["inspected_count"] == 3
        assert result["imported_jira_count"] == 2
        assert result["candidate_count"] == 1
        assert result["updated_count"] == 0
        assert result["sample_task_concept_ids"] == ["#V#task_labelled_import"]
        mock_repo.update_one.assert_not_called()


class TestDeleteTask:
    """Tests for delete_task function."""

    @patch("src.backend.services.task_management_service.update_task_status")
    @patch("src.backend.services.task_management_service.ConceptsRepository")
    def test_delete_task_success(
        self,
        mock_repo: MagicMock,
        mock_update_status: MagicMock,
    ) -> None:
        """delete_task() should set status to cancelled."""
        mock_repo.find_one.return_value = {
            "concept_id": "#V#task_abc",
            "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
        }

        result = delete_task("#V#task_abc")

        assert result is True
        mock_update_status.assert_called_once_with("#V#task_abc", TASK_STATUS_CANCELLED)

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    def test_delete_task_not_found(self, mock_repo: MagicMock) -> None:
        """delete_task() should raise TaskNotFoundError if not found."""
        mock_repo.find_one.return_value = None

        with pytest.raises(TaskNotFoundError):
            delete_task("#V#task_nonexistent")


class TestTaskParityDatesAndEpic:
    """Coverage for Jira-parity start-date and epic-link task fields."""

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.upsert_text_for_concept")
    @patch("src.backend.services.task_management_service._get_task_doc")
    @patch(
        "src.backend.services.task_management_service.maybe_launch_task_created_workflow"
    )
    def test_create_task_stores_start_date_and_epic(
        self,
        mock_launch_workflow: MagicMock,
        mock_get_task_doc: MagicMock,
        mock_upsert: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        mock_repo.insert_one.return_value = None
        mock_get_task_doc.return_value = (
            "#V#task_epic_1",
            {
                "concept_id": "#V#task_epic_1",
                "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
            },
        )

        start_date = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        due_date = datetime(2026, 3, 5, 10, 0, 0, tzinfo=timezone.utc)
        result = create_task(
            title="Task with dates",
            description="Description",
            start_date=start_date,
            due_date=due_date,
            epic_task_concept_id="#V#task_epic_1",
        )

        assert result["start_date"] == "2026-03-01T10:00:00+00:00"
        assert result["due_date"] == "2026-03-05T10:00:00+00:00"
        assert result["epic_task_concept_id"] == "#V#task_epic_1"
        inserted_doc = mock_repo.insert_one.call_args.args[0]
        assert inserted_doc["relationships"]["#V#hasEpicTask"] == ["#V#task_epic_1"]
        predicates = [
            call.kwargs.get("predicate") for call in mock_upsert.call_args_list
        ]
        assert "#V#hasStartDate" in predicates
        assert "#V#hasDueDate" in predicates
        mock_launch_workflow.assert_called_once()

    def test_create_task_rejects_start_after_due(self) -> None:
        with pytest.raises(
            InvalidTaskDataError, match="start_date must be before or equal to due_date"
        ):
            create_task(
                title="Invalid date order",
                description="Description",
                start_date="2026-03-10T00:00:00Z",
                due_date="2026-03-01T00:00:00Z",
            )

    @patch("src.backend.services.task_management_service.get_task")
    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    @patch("src.backend.services.task_management_service.upsert_text_for_concept")
    def test_update_task_fields_start_date_and_epic(
        self,
        mock_upsert: MagicMock,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
        mock_get_task: MagicMock,
    ) -> None:
        task_doc = {
            "concept_id": "#V#task_1",
            "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
            "metadata": {},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        epic_doc = {
            "concept_id": "#V#task_epic_1",
            "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
            "metadata": {},
        }

        def _fake_find_one(
            query: Dict[str, Any], projection: Optional[Dict[str, Any]] = None
        ):
            concept_id = query.get("concept_id")
            if concept_id == "#V#task_epic_1":
                return epic_doc
            if concept_id == "#V#task_1":
                return task_doc
            return None

        mock_repo.find_one.side_effect = _fake_find_one
        mock_get_texts.return_value = []
        mock_get_task.return_value = {
            "task_concept_id": "#V#task_1",
            "start_date": "2026-03-01T10:00:00+00:00",
            "epic_task_concept_id": "#V#task_epic_1",
        }

        result = update_task_fields(
            "#V#task_1",
            fields={
                "start_date": "2026-03-01T10:00:00Z",
                "epic_task_concept_id": "#V#task_epic_1",
            },
            actor_concept_id="#V#user_alice",
        )

        assert "start_date" in result["changed_fields"]
        assert "epic_task_concept_id" in result["changed_fields"]
        assert result["task"]["epic_task_concept_id"] == "#V#task_epic_1"
        assert any(
            call.kwargs.get("predicate") == "#V#hasStartDate"
            for call in mock_upsert.call_args_list
        )
        assert any(
            call.kwargs.get("kind") == "#V#hasEpicTask"
            and call.kwargs.get("action") == "add"
            for call in mock_repo.mutate_relationship_edge.call_args_list
        )

    @patch("src.backend.services.task_management_service.get_task")
    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    def test_update_task_fields_supports_planning_metadata(
        self,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
        mock_get_task: MagicMock,
    ) -> None:
        task_doc = {
            "concept_id": "#V#task_1",
            "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
            "metadata": {},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        mock_repo.find_one.return_value = task_doc
        mock_get_texts.return_value = []
        mock_get_task.return_value = {
            "task_concept_id": "#V#task_1",
            "components": ["Workflow Engine"],
            "fix_versions": ["R1"],
            "sprint_values": ["Sprint 6"],
            "backlog_rank": "0|i00123:",
        }

        result = update_task_fields(
            "#V#task_1",
            fields={
                "components": ["Workflow Engine"],
                "fix_versions": ["R1"],
                "sprint_values": ["Sprint 6"],
                "backlog_rank": "0|i00123:",
            },
            actor_concept_id="#V#user_alice",
        )

        assert "components" in result["changed_fields"]
        assert "fix_versions" in result["changed_fields"]
        assert "sprint_values" in result["changed_fields"]
        assert "backlog_rank" in result["changed_fields"]
        update_payloads = [
            call.args[1]
            for call in mock_repo.update_one.call_args_list
            if len(call.args) >= 2 and isinstance(call.args[1], dict)
        ]
        set_payloads = [
            payload.get("$set", {})
            for payload in update_payloads
            if isinstance(payload.get("$set"), dict)
        ]
        assert any("metadata.components" in payload for payload in set_payloads)
        assert any("metadata.fix_versions" in payload for payload in set_payloads)
        assert any("metadata.sprint_values" in payload for payload in set_payloads)
        assert any("metadata.backlog_rank" in payload for payload in set_payloads)

    @patch("src.backend.services.task_management_service.get_task")
    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    @patch("src.backend.services.task_management_service.upsert_text_for_concept")
    def test_update_task_fields_supports_task_taxonomy_context(
        self,
        mock_upsert: MagicMock,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
        mock_get_task: MagicMock,
    ) -> None:
        task_doc = {
            "concept_id": "#V#task_1",
            "relationships": {
                "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID, DEFAULT_TASK_TYPE_ID],
                PREDICATE_HAS_TASK_SOURCE: [DEFAULT_TASK_SOURCE_ID],
            },
            "metadata": {},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        mock_repo.find_one.return_value = task_doc
        mock_get_texts.return_value = []
        mock_get_task.return_value = {
            "task_concept_id": "#V#task_1",
            "task_type_ids": ["#V#delegated_task_specification"],
            "task_source_id": JIRA_IMPORTED_TASK_SOURCE_ID,
            "report_to_concept_id": "#V#user_manager",
            "task_role": "Communicator",
            "next_checkpoint": "Tomorrow morning",
            "progress_signal": "Confirmed by chat",
            "evidence": "Printed document",
            "notes": "Needs a coloured copy",
            "reference_code": "TASK-001",
        }

        result = update_task_fields(
            "#V#task_1",
            fields={
                "task_type_ids": ["#V#delegated_task_specification"],
                "task_source_id": JIRA_IMPORTED_TASK_SOURCE_ID,
                "report_to_concept_id": "#V#user_manager",
                "task_role": "Communicator",
                "next_checkpoint": "Tomorrow morning",
                "progress_signal": "Confirmed by chat",
                "evidence": "Printed document",
                "notes": "Needs a coloured copy",
                "reference_code": "TASK-001",
            },
            actor_concept_id="#V#user_alice",
        )

        assert "task_type_ids" in result["changed_fields"]
        assert "task_source_id" in result["changed_fields"]
        assert "report_to_concept_id" in result["changed_fields"]
        assert "task_role" in result["changed_fields"]
        assert "next_checkpoint" in result["changed_fields"]
        assert "progress_signal" in result["changed_fields"]
        assert "evidence" in result["changed_fields"]
        assert "notes" in result["changed_fields"]
        assert "reference_code" in result["changed_fields"]
        assert any(
            call.kwargs.get("kind") == "is_an_instance_of"
            and call.kwargs.get("target_id") == "#V#delegated_task_specification"
            and call.kwargs.get("action") == "add"
            for call in mock_repo.mutate_relationship_edge.call_args_list
        )
        assert any(
            call.kwargs.get("kind") == PREDICATE_REPORTS_TO
            and call.kwargs.get("target_id") == "#V#user_manager"
            and call.kwargs.get("action") == "add"
            for call in mock_repo.mutate_relationship_edge.call_args_list
        )
        stored_predicates = {
            call.kwargs.get("predicate") for call in mock_upsert.call_args_list
        }
        assert PREDICATE_HAS_TASK_ROLE in stored_predicates
        assert PREDICATE_HAS_NEXT_CHECKPOINT in stored_predicates
        assert PREDICATE_HAS_PROGRESS_SIGNAL in stored_predicates
        assert PREDICATE_HAS_EVIDENCE in stored_predicates
        assert PREDICATE_HAS_TASK_REFERENCE_CODE in stored_predicates

    @patch("src.backend.services.task_management_service.get_task")
    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    def test_update_task_fields_supports_creator_reporter_and_watchers(
        self,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
        mock_get_task: MagicMock,
    ) -> None:
        task_doc = {
            "concept_id": "#V#task_1",
            "relationships": {
                "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID],
                "#V#hasCreatedBy": ["#V#user_old_creator"],
            },
            "metadata": {},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

        known_people = {
            "#V#user_creator",
            "#V#user_reporter",
            "#V#user_watcher_1",
            "#V#user_watcher_2",
            "#V#sail_org",
        }

        def _fake_find_one(
            query: Dict[str, Any], projection: Optional[Dict[str, Any]] = None
        ):
            concept_id = query.get("concept_id")
            if concept_id == "#V#task_1":
                return task_doc
            if concept_id in known_people:
                return {"concept_id": concept_id}
            return None

        mock_repo.find_one.side_effect = _fake_find_one
        mock_get_texts.return_value = []
        mock_get_task.return_value = {
            "task_concept_id": "#V#task_1",
            "created_by_concept_id": "#V#user_creator",
            "reporter_concept_id": "#V#user_reporter",
            "watcher_concept_ids": ["#V#user_watcher_1", "#V#user_watcher_2"],
            "organisation_concept_id": "#V#sail_org",
        }

        result = update_task_fields(
            "#V#task_1",
            fields={
                "created_by_concept_id": "#V#user_creator",
                "reporter_concept_id": "#V#user_reporter",
                "watcher_concept_ids": ["#V#user_watcher_1", "#V#user_watcher_2"],
                "organisation_concept_id": "#V#sail_org",
            },
            actor_concept_id="#V#user_alice",
        )

        assert "created_by_concept_id" in result["changed_fields"]
        assert "reporter_concept_id" in result["changed_fields"]
        assert "watcher_concept_ids" in result["changed_fields"]
        assert "organisation_concept_id" in result["changed_fields"]
        assert any(
            call.kwargs.get("kind") == "#V#hasCreatedBy"
            and call.kwargs.get("target_id") == "#V#user_creator"
            and call.kwargs.get("action") == "add"
            for call in mock_repo.mutate_relationship_edge.call_args_list
        )

        update_payloads = [
            call.args[1]
            for call in mock_repo.update_one.call_args_list
            if len(call.args) >= 2 and isinstance(call.args[1], dict)
        ]
        set_payloads = [
            payload.get("$set", {})
            for payload in update_payloads
            if isinstance(payload.get("$set"), dict)
        ]
        assert any(
            "metadata.jira_reporter_concept_id" in payload for payload in set_payloads
        )
        assert any(
            "metadata.jira_watcher_concept_ids" in payload for payload in set_payloads
        )
        assert any(
            "metadata.organisation_concept_id" in payload for payload in set_payloads
        )
        assert any(
            payload.get("relationships.specific_to_org") == ["#V#sail_org"]
            for payload in set_payloads
        )

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    def test_search_tasks_pagination_is_stable_across_offsets(
        self,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        docs = [
            {
                "concept_id": f"#V#task_{idx:03d}",
                "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
                "metadata": {},
                "created_at": base + timedelta(minutes=idx),
                "updated_at": base + timedelta(minutes=idx),
            }
            for idx in range(1, 251)
        ]

        def _fake_find(
            _filter: Dict[str, Any],
            projection: Optional[Dict[str, Any]] = None,  # noqa: ARG001
            sort: Optional[List] = None,  # noqa: ARG001
            skip: int = 0,
            limit: int = 0,
        ):
            rows = list(docs)
            if skip:
                rows = rows[skip:]
            if limit:
                rows = rows[:limit]
            return rows

        def _fake_get_texts(
            concept_id: str, *args: Any, **kwargs: Any  # noqa: ARG001
        ) -> list[dict[str, str]]:
            return [
                {"predicate": "#V#hasName", "text": concept_id},
                {"predicate": "#V#hasTaskStatus", "text": "pending"},
            ]

        mock_repo.find.side_effect = _fake_find
        mock_get_texts.side_effect = _fake_get_texts

        page_one = search_tasks(limit=200, offset=0)
        page_two = search_tasks(limit=200, offset=200)

        page_one_ids = [task["task_concept_id"] for task in page_one["tasks"]]
        page_two_ids = [task["task_concept_id"] for task in page_two["tasks"]]

        assert page_one["count"] == 200
        assert page_two["count"] == 50
        assert set(page_one_ids).isdisjoint(page_two_ids)
        assert len(set(page_one_ids + page_two_ids)) == 250

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    def test_search_tasks_filters_start_date_and_epic(
        self,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        now = datetime.now(timezone.utc)
        mock_repo.find.return_value = [
            {
                "concept_id": "#V#task_1",
                "relationships": {
                    "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID],
                    "#V#hasEpicTask": ["#V#task_epic_1"],
                },
                "metadata": {},
                "created_at": now,
                "updated_at": now,
            },
            {
                "concept_id": "#V#task_2",
                "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
                "metadata": {},
                "created_at": now,
                "updated_at": now,
            },
        ]

        def _fake_get_texts(
            concept_id: str, *args: Any, **kwargs: Any
        ) -> list[dict[str, str]]:
            if concept_id == "#V#task_1":
                return [
                    {"predicate": "#V#hasName", "text": "Task 1"},
                    {
                        "predicate": "#V#hasStartDate",
                        "text": "2026-03-02T00:00:00+00:00",
                    },
                    {"predicate": "#V#hasTaskStatus", "text": "pending"},
                ]
            return [
                {"predicate": "#V#hasName", "text": "Task 2"},
                {"predicate": "#V#hasStartDate", "text": "2026-04-10T00:00:00+00:00"},
                {"predicate": "#V#hasTaskStatus", "text": "pending"},
            ]

        mock_get_texts.side_effect = _fake_get_texts

        result = search_tasks(
            epic_task_concept_id="#V#task_epic_1",
            start_from="2026-03-01T00:00:00Z",
            start_to="2026-03-05T00:00:00Z",
            limit=10,
        )

        assert result["count"] == 1
        assert result["tasks"][0]["task_concept_id"] == "#V#task_1"

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    def test_search_tasks_filters_planning_metadata(
        self,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        now = datetime.now(timezone.utc)
        mock_repo.find.return_value = [
            {
                "concept_id": "#V#task_1",
                "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
                "metadata": {
                    "components": ["Workflow Engine"],
                    "fix_versions": ["R1"],
                    "sprint_values": ["Sprint 6"],
                    "backlog_rank": "0|i00123:",
                },
                "created_at": now,
                "updated_at": now,
            },
            {
                "concept_id": "#V#task_2",
                "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
                "metadata": {
                    "components": ["Frontend Chat UX"],
                    "fix_versions": ["R2"],
                    "sprint_values": ["Sprint 7"],
                    "backlog_rank": None,
                },
                "created_at": now,
                "updated_at": now,
            },
        ]

        def _fake_get_texts(
            concept_id: str, *args: Any, **kwargs: Any
        ) -> list[dict[str, str]]:
            return [
                {"predicate": "#V#hasName", "text": concept_id},
                {"predicate": "#V#hasTaskStatus", "text": "pending"},
            ]

        mock_get_texts.side_effect = _fake_get_texts

        result = search_tasks(
            components=["Workflow Engine"],
            fix_versions=["R1"],
            sprint_values=["Sprint 6"],
            has_backlog_rank=True,
            limit=10,
        )

        assert result["count"] == 1
        assert result["tasks"][0]["task_concept_id"] == "#V#task_1"

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    def test_search_tasks_filters_task_taxonomy_and_source(
        self,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        now = datetime.now(timezone.utc)
        mock_repo.find.return_value = [
            {
                "concept_id": "#V#task_1",
                "relationships": {
                    "is_an_instance_of": [
                        TASK_SPECIFICATION_TYPE_ID,
                        "#V#delegated_task_specification",
                    ],
                    "#V#hasCreatedBy": ["#V#user_creator"],
                    PREDICATE_HAS_TASK_SOURCE: [JIRA_IMPORTED_TASK_SOURCE_ID],
                    PREDICATE_REPORTS_TO: ["#V#user_manager"],
                },
                "metadata": {},
                "created_at": now,
                "updated_at": now,
            },
            {
                "concept_id": "#V#task_2",
                "relationships": {
                    "is_an_instance_of": [
                        TASK_SPECIFICATION_TYPE_ID,
                        DEFAULT_TASK_TYPE_ID,
                    ],
                    "#V#hasCreatedBy": ["#V#other_user"],
                    PREDICATE_HAS_TASK_SOURCE: [DEFAULT_TASK_SOURCE_ID],
                    PREDICATE_REPORTS_TO: ["#V#user_other_manager"],
                },
                "metadata": {},
                "created_at": now,
                "updated_at": now,
            },
        ]

        def _fake_get_texts(
            concept_id: str, *args: Any, **kwargs: Any
        ) -> list[dict[str, str]]:
            return [
                {"predicate": "#V#hasName", "text": concept_id},
                {"predicate": "#V#hasTaskStatus", "text": "pending"},
            ]

        mock_get_texts.side_effect = _fake_get_texts

        result = search_tasks(
            task_type_ids=["#V#delegated_task_specification"],
            task_source_id=JIRA_IMPORTED_TASK_SOURCE_ID,
            created_by_concept_id="#V#user_creator",
            report_to_concept_id="#V#user_manager",
            limit=10,
        )

        assert result["count"] == 1
        assert result["tasks"][0]["task_concept_id"] == "#V#task_1"


class TestTaskExternalReferences:
    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    def test_find_task_by_external_reference_returns_task(
        self,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        now = datetime.now(timezone.utc)
        mock_repo.find_one.return_value = {
            "concept_id": "#V#task_imported_1",
            "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
            "metadata": {
                "external_references": {
                    "jira": {
                        "external_id": "JVNAUTOSCI-777",
                    }
                }
            },
            "created_at": now,
            "updated_at": now,
        }
        mock_get_texts.return_value = [
            {"predicate": "#V#hasName", "text": "Imported task"},
            {"predicate": "#V#hasTaskStatus", "text": "pending"},
        ]

        result = find_task_by_external_reference(
            source_system="jira",
            external_id="JVNAUTOSCI-777",
        )

        assert result is not None
        assert result["task_concept_id"] == "#V#task_imported_1"
        assert (
            result.get("external_references", {}).get("jira", {}).get("external_id")
            == "JVNAUTOSCI-777"
        )

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.get_texts_for_concept")
    def test_find_task_by_external_reference_org_lookup_falls_back_to_legacy_unscoped(
        self,
        mock_get_texts: MagicMock,
        mock_repo: MagicMock,
    ) -> None:
        now = datetime.now(timezone.utc)
        legacy_doc = {
            "concept_id": "#V#task_imported_legacy",
            "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
            "metadata": {
                "external_references": {"jira": {"external_id": "JVNAUTOSCI-888"}},
                "organisation_concept_id": None,
            },
            "created_at": now,
            "updated_at": now,
        }
        mock_repo.find_one.side_effect = [None, legacy_doc]
        mock_get_texts.return_value = [
            {"predicate": "#V#hasName", "text": "Imported legacy task"},
            {"predicate": "#V#hasTaskStatus", "text": "pending"},
        ]

        result = find_task_by_external_reference(
            source_system="jira",
            external_id="JVNAUTOSCI-888",
            organisation_concept_id="#V#sail_org",
        )

        assert result is not None
        assert result["task_concept_id"] == "#V#task_imported_legacy"
        assert mock_repo.find_one.call_count == 2
        first_query = mock_repo.find_one.call_args_list[0].args[0]
        second_query = mock_repo.find_one.call_args_list[1].args[0]
        assert first_query.get("metadata.organisation_concept_id") == "#V#sail_org"
        assert "metadata.organisation_concept_id" in second_query
        assert second_query.get("metadata.organisation_concept_id") is None

    @patch("src.backend.services.task_management_service.get_task")
    @patch("src.backend.services.task_management_service._get_task_doc")
    @patch("src.backend.services.task_management_service.ConceptsRepository")
    def test_upsert_task_external_reference_updates_metadata(
        self,
        mock_repo: MagicMock,
        mock_get_task_doc: MagicMock,
        mock_get_task: MagicMock,
    ) -> None:
        mock_get_task_doc.return_value = ("#V#task_imported_1", {})
        mock_get_task.return_value = {"task_concept_id": "#V#task_imported_1"}

        result = upsert_task_external_reference(
            "#V#task_imported_1",
            source_system="jira",
            external_id="JVNAUTOSCI-888",
            reference_payload={"status_name": "In Progress"},
        )

        assert result["task_concept_id"] == "#V#task_imported_1"
        update_payloads = [
            call.args[1]
            for call in mock_repo.update_one.call_args_list
            if len(call.args) >= 2 and isinstance(call.args[1], dict)
        ]
        metadata_updates = [
            payload
            for payload in update_payloads
            if "metadata.external_references.jira" in payload.get("$set", {})
        ]
        assert metadata_updates
        assert (
            metadata_updates[0]["$set"]["metadata.external_references.jira"][
                "external_id"
            ]
            == "JVNAUTOSCI-888"
        )


class TestImportedTaskActivity:
    @patch("src.backend.services.task_management_service._get_task_doc")
    @patch("src.backend.services.task_management_service.ConceptsRepository")
    def test_comment_attachment_and_worklog_preserve_source_metadata(
        self,
        mock_repo: MagicMock,
        mock_get_task_doc: MagicMock,
    ) -> None:
        mock_get_task_doc.return_value = ("#V#task_1", {})

        add_task_comment(
            "#V#task_1",
            body="Imported comment",
            author_concept_id="#V#user_commenter",
            created_at="2026-04-01T10:00:00Z",
            source={"source_system": "jira", "external_id": "10001"},
        )
        add_task_attachment(
            "#V#task_1",
            filename="spec.pdf",
            uri="s3://bucket/spec.pdf",
            added_by_concept_id="#V#user_attacher",
            created_at="2026-04-01T10:05:00Z",
            source={"source_system": "jira", "external_id": "20001"},
            file_copy_concept_id="#V#computer_file_copy_1",
        )
        add_task_worklog(
            "#V#task_1",
            time_spent_minutes=2,
            author_concept_id="#V#user_worker",
            created_at="2026-04-01T10:10:00Z",
            started_at="2026-04-01T09:30:00Z",
            source={"source_system": "jira", "external_id": "30001"},
        )
        record_task_history_event(
            "#V#task_1",
            event_type="task_status_transition_imported",
            actor_concept_id="#V#user_commenter",
            event_timestamp="2026-04-01T09:00:00Z",
            details={"source_system": "jira", "external_id": "history-1"},
        )

        update_payloads = [
            call.args[1]
            for call in mock_repo.update_one.call_args_list
            if len(call.args) >= 2 and isinstance(call.args[1], dict)
        ]
        pushed_entries = [
            payload.get("$push", {})
            for payload in update_payloads
            if isinstance(payload.get("$push"), dict)
        ]
        assert any(
            entry.get("metadata.comments", {}).get("source", {}).get("external_id")
            == "10001"
            for entry in pushed_entries
        )
        assert any(
            entry.get("metadata.attachments", {}).get("file_copy_concept_id")
            == "#V#computer_file_copy_1"
            for entry in pushed_entries
        )
        assert any(
            entry.get("metadata.worklog", {}).get("source", {}).get("external_id")
            == "30001"
            for entry in pushed_entries
        )
        assert any(
            entry.get("metadata.task_history", {}).get("timestamp")
            == "2026-04-01T09:00:00+00:00"
            for entry in pushed_entries
        )
