"""Tests for task management service (JVNAUTOSCI-1040).

Unit tests for task creation, retrieval, status updates, and assignment.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
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
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    PRIORITY_HIGH,
    TaskManagementError,
    TaskNotFoundError,
    InvalidTaskDataError,
    create_task,
    get_task,
    update_task_status,
    assign_task,
    get_tasks_for_user,
    list_tasks,
    delete_task,
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
    def test_create_task_with_minimal_params(
        self,
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

    @patch("src.backend.services.task_management_service.ConceptsRepository")
    @patch("src.backend.services.task_management_service.upsert_text_for_concept")
    @patch("src.backend.services.conversation_concept_service.get_or_create_conversation_concept")
    def test_create_task_with_all_params(
        self,
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
        )

        assert result["title"] == "Full Task"
        assert result["status"] == TASK_STATUS_PENDING
        assert result["priority"] == "high"
        assert result["assignee_concept_id"] == "#V#user_alice"

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
    @patch("src.backend.services.task_management_service.upsert_text_for_concept")
    def test_update_status_valid(
        self,
        mock_upsert: MagicMock,
        mock_repo: MagicMock,
        mock_get_task: MagicMock,
    ) -> None:
        """update_task_status() with valid status should update."""
        mock_repo.find_one.return_value = {
            "concept_id": "#V#task_abc",
            "relationships": {"is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID]},
        }
        mock_get_task.return_value = {
            "task_concept_id": "#V#task_abc",
            "status": "in_progress",
        }

        result = update_task_status("#V#task_abc", "in_progress")

        assert result["status"] == "in_progress"
        mock_upsert.assert_called()

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
