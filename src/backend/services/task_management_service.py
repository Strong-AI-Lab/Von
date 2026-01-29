"""Task management service for JVNAUTOSCI-1040.

Provides CRUD operations for task concepts, linking tasks to conversations,
assignees, and tracking status via Vontology relationships and text relations.

All tasks are stored as first-class Vontology concepts (type: #V#task_specification).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..db.repositories.concepts_repository import ConceptsRepository
from ..services.text_value_service import (
    get_texts_for_concept,
    upsert_text_for_concept,
)
from ..utils.concept_id_utils import (
    canonicalise_vontology_concept_id,
    ensure_v_concept_prefix,
)

logger = logging.getLogger(__name__)

# Task type concept IDs
TASK_SPECIFICATION_TYPE_ID = "#V#task_specification"
TASK_EXECUTION_TYPE_ID = "#V#task_execution"
CONVERSATION_TYPE_ID = "#V#conversation"

# Task-related predicates
PREDICATE_HAS_ASSIGNEE = "#V#hasAssignee"
PREDICATE_HAS_CREATED_BY = "#V#hasCreatedBy"
PREDICATE_HAS_ORIGINATING_CONVERSATION = "#V#hasOriginatingConversation"
PREDICATE_HAS_DUE_DATE = "#V#hasDueDate"
PREDICATE_HAS_PRIORITY = "#V#hasPriority"
PREDICATE_HAS_TASK_STATUS = "#V#hasTaskStatus"
PREDICATE_HAS_DESCRIPTION = "#V#hasDescription"
PREDICATE_HAS_NAME = "#V#hasName"

# Valid task statuses
TASK_STATUS_PENDING = "pending"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_CANCELLED = "cancelled"
TASK_STATUS_BLOCKED = "blocked"

VALID_TASK_STATUSES = {
    TASK_STATUS_PENDING,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_BLOCKED,
}

# Valid priorities
PRIORITY_LOW = "low"
PRIORITY_MEDIUM = "medium"
PRIORITY_HIGH = "high"
PRIORITY_CRITICAL = "critical"

VALID_PRIORITIES = {PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH, PRIORITY_CRITICAL}


class TaskManagementError(Exception):
    """Base exception for task management operations."""

    pass


class TaskNotFoundError(TaskManagementError):
    """Task concept not found."""

    pass


class InvalidTaskDataError(TaskManagementError):
    """Invalid task data provided."""

    pass


def _generate_task_concept_id(title: str) -> str:
    """Generate a unique concept_id for a task.

    Uses a slug from the title plus a short UUID suffix for uniqueness.
    """
    # Create slug from title
    slug = title.lower().strip()
    slug = "".join(c if c.isalnum() or c == " " else "" for c in slug)
    slug = "_".join(slug.split()[:5])  # Max 5 words
    if not slug:
        slug = "task"

    # Add short UUID suffix
    short_uuid = uuid.uuid4().hex[:8]
    concept_id = f"#V#task_{slug}_{short_uuid}"

    return concept_id


def create_task(
    title: str,
    description: str,
    *,
    assignee_concept_id: Optional[str] = None,
    originating_session_id: Optional[str] = None,
    created_by_concept_id: Optional[str] = None,
    due_date: Optional[datetime] = None,
    priority: str = PRIORITY_MEDIUM,
    organisation_concept_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new task as a Vontology concept.

    Args:
        title: Task title (will be stored as hasName text relation)
        description: Task description (will be stored as hasDescription text relation)
        assignee_concept_id: Person/org concept_id responsible for the task
        originating_session_id: Chat session_id where task was created
        created_by_concept_id: Person/agent who created the task
        due_date: Optional deadline
        priority: Task priority (low, medium, high, critical)
        organisation_concept_id: Organisation context, if applicable

    Returns:
        Dict with task_concept_id, title, status, and other metadata

    Raises:
        InvalidTaskDataError: If required data is missing or invalid
    """
    if not title or not isinstance(title, str):
        raise InvalidTaskDataError("Task title is required")

    if not description or not isinstance(description, str):
        raise InvalidTaskDataError("Task description is required")

    if priority not in VALID_PRIORITIES:
        raise InvalidTaskDataError(
            f"Invalid priority '{priority}'. Must be one of: {VALID_PRIORITIES}"
        )

    # Generate unique concept_id
    task_concept_id = _generate_task_concept_id(title)

    # Normalise concept IDs
    if assignee_concept_id:
        assignee_concept_id = ensure_v_concept_prefix(assignee_concept_id)
    if created_by_concept_id:
        created_by_concept_id = ensure_v_concept_prefix(created_by_concept_id)
    if organisation_concept_id:
        organisation_concept_id = ensure_v_concept_prefix(organisation_concept_id)

    now = datetime.now(timezone.utc)

    # Build relationships
    relationships: Dict[str, Any] = {
        "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID],
    }

    if assignee_concept_id:
        relationships[PREDICATE_HAS_ASSIGNEE] = [assignee_concept_id]
    if created_by_concept_id:
        relationships[PREDICATE_HAS_CREATED_BY] = [created_by_concept_id]

    # Handle conversation linkage (lazy creation)
    conversation_concept_id = None
    if originating_session_id:
        from .conversation_concept_service import get_or_create_conversation_concept

        conversation_concept_id = get_or_create_conversation_concept(
            session_id=originating_session_id,
            owner_concept_id=created_by_concept_id,
            organisation_concept_id=organisation_concept_id,
        )
        if conversation_concept_id:
            relationships[PREDICATE_HAS_ORIGINATING_CONVERSATION] = [
                conversation_concept_id
            ]

    # Create the concept document
    concept_doc: Dict[str, Any] = {
        "concept_id": task_concept_id,
        "guid": str(uuid.uuid4()),
        "relationships": relationships,
        "created_at": now,
        "updated_at": now,
        "metadata": {
            "concept_type": "task_specification",
            "organisation_concept_id": organisation_concept_id,
        },
    }

    try:
        ConceptsRepository.insert_one(concept_doc)
        logger.info(f"Created task concept: {task_concept_id}")
    except Exception as e:
        logger.error(f"Failed to create task concept: {e}")
        raise TaskManagementError(f"Failed to create task: {e}") from e

    # Store title as hasName text relation
    try:
        upsert_text_for_concept(
            subject_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_NAME,
            text=title,
            lang="en-NZ",
        )
    except Exception as e:
        logger.warning(f"Failed to store task title: {e}")

    # Store description as hasDescription text relation
    try:
        upsert_text_for_concept(
            subject_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_DESCRIPTION,
            text=description,
            lang="en-NZ",
        )
    except Exception as e:
        logger.warning(f"Failed to store task description: {e}")

    # Store status as text relation
    try:
        upsert_text_for_concept(
            subject_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_TASK_STATUS,
            text=TASK_STATUS_PENDING,
            lang="en",
        )
    except Exception as e:
        logger.warning(f"Failed to store task status: {e}")

    # Store priority as text relation
    try:
        upsert_text_for_concept(
            subject_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_PRIORITY,
            text=priority,
            lang="en",
        )
    except Exception as e:
        logger.warning(f"Failed to store task priority: {e}")

    # Store due date if provided
    if due_date:
        try:
            upsert_text_for_concept(
                subject_concept_id=task_concept_id,
                predicate=PREDICATE_HAS_DUE_DATE,
                text=due_date.isoformat(),
                lang="en",
            )
        except Exception as e:
            logger.warning(f"Failed to store task due date: {e}")

    return {
        "task_concept_id": task_concept_id,
        "title": title,
        "description": description,
        "status": TASK_STATUS_PENDING,
        "priority": priority,
        "assignee_concept_id": assignee_concept_id,
        "created_by_concept_id": created_by_concept_id,
        "originating_conversation_id": conversation_concept_id,
        "due_date": due_date.isoformat() if due_date else None,
        "organisation_concept_id": organisation_concept_id,
        "created_at": now.isoformat(),
    }


def get_task(task_concept_id: str) -> Dict[str, Any]:
    """Get a task by concept_id.

    Args:
        task_concept_id: The task's concept_id

    Returns:
        Dict with task details

    Raises:
        TaskNotFoundError: If task not found
    """
    normalised_id = ensure_v_concept_prefix(task_concept_id)
    if not normalised_id:
        raise TaskNotFoundError(f"Invalid task_concept_id: {task_concept_id}")
    task_concept_id = normalised_id

    doc = ConceptsRepository.find_one({"concept_id": task_concept_id})
    if not doc:
        raise TaskNotFoundError(f"Task not found: {task_concept_id}")

    # Check it's actually a task
    relationships = doc.get("relationships", {})
    instance_of = relationships.get("is_an_instance_of", [])
    if TASK_SPECIFICATION_TYPE_ID not in instance_of:
        raise TaskNotFoundError(f"Concept is not a task: {task_concept_id}")

    return _build_task_response(doc)


def _build_task_response(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Build a task response dict from a concept document."""
    task_concept_id = doc.get("concept_id", "")
    relationships = doc.get("relationships", {})

    # Get text relations for title, description, status, priority, due date
    texts = []
    try:
        texts = get_texts_for_concept(task_concept_id) or []
    except Exception:
        pass

    title = None
    description = None
    status = TASK_STATUS_PENDING
    priority = PRIORITY_MEDIUM
    due_date = None

    for text_item in texts:
        predicate = text_item.get("predicate", "")
        text_value = text_item.get("text", "")

        if predicate in (PREDICATE_HAS_NAME, "hasName"):
            title = text_value
        elif predicate in (PREDICATE_HAS_DESCRIPTION, "hasDescription"):
            description = text_value
        elif predicate in (PREDICATE_HAS_TASK_STATUS, "hasTaskStatus"):
            status = text_value
        elif predicate in (PREDICATE_HAS_PRIORITY, "hasPriority"):
            priority = text_value
        elif predicate in (PREDICATE_HAS_DUE_DATE, "hasDueDate"):
            due_date = text_value

    # Get relationship values
    assignee = (relationships.get(PREDICATE_HAS_ASSIGNEE) or [None])[0]
    created_by = (relationships.get(PREDICATE_HAS_CREATED_BY) or [None])[0]
    originating_conversation = (
        relationships.get(PREDICATE_HAS_ORIGINATING_CONVERSATION) or [None]
    )[0]

    metadata = doc.get("metadata", {})

    # Fetch conversation details if available
    conversation_session_id = None
    conversation_name = None
    if originating_conversation:
        try:
            from .conversation_concept_service import get_conversation_concept

            conv_details = get_conversation_concept(originating_conversation)
            if conv_details:
                conversation_session_id = conv_details.get("session_id")
                conversation_name = conv_details.get("name") or conv_details.get(
                    "topic"
                )
        except Exception:
            pass

        # Fallback: get session name from chat_history if not in conversation concept
        if conversation_session_id and not conversation_name:
            try:
                from .chat_history_service import get_chat_history_collection_service

                chat_history_coll = get_chat_history_collection_service()
                if chat_history_coll is not None:
                    # Query by session_id (any user's session with that ID)
                    session_doc = chat_history_coll.find_one(
                        {"session_id": conversation_session_id},
                        {"session_name": 1},
                    )
                    if session_doc and session_doc.get("session_name"):
                        conversation_name = session_doc["session_name"]
            except Exception:
                pass

    return {
        "task_concept_id": task_concept_id,
        "title": title or doc.get("name", "Untitled Task"),
        "description": description,
        "status": status,
        "priority": priority,
        "assignee_concept_id": assignee,
        "created_by_concept_id": created_by,
        "originating_conversation_id": originating_conversation,
        "conversation_session_id": conversation_session_id,
        "conversation_name": conversation_name,
        "due_date": due_date,
        "organisation_concept_id": metadata.get("organisation_concept_id"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


def update_task_status(task_concept_id: str, status: str) -> Dict[str, Any]:
    """Update a task's status.

    Args:
        task_concept_id: The task's concept_id
        status: New status (pending, in_progress, completed, cancelled, blocked)

    Returns:
        Updated task dict

    Raises:
        TaskNotFoundError: If task not found
        InvalidTaskDataError: If status is invalid
    """
    if status not in VALID_TASK_STATUSES:
        raise InvalidTaskDataError(
            f"Invalid status '{status}'. Must be one of: {VALID_TASK_STATUSES}"
        )

    normalised_id = ensure_v_concept_prefix(task_concept_id)
    if not normalised_id:
        raise TaskNotFoundError(f"Invalid task_concept_id: {task_concept_id}")
    task_concept_id = normalised_id

    # Verify task exists
    doc = ConceptsRepository.find_one({"concept_id": task_concept_id})
    if not doc:
        raise TaskNotFoundError(f"Task not found: {task_concept_id}")

    # Update status text relation
    try:
        upsert_text_for_concept(
            subject_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_TASK_STATUS,
            text=status,
            lang="en",
        )
    except Exception as e:
        raise TaskManagementError(f"Failed to update task status: {e}") from e

    # Update timestamp
    now = datetime.now(timezone.utc)
    ConceptsRepository.update_one(
        {"concept_id": task_concept_id},
        {"$set": {"updated_at": now}},
    )

    logger.info(f"Updated task {task_concept_id} status to {status}")
    return get_task(task_concept_id)


def assign_task(task_concept_id: str, assignee_concept_id: str) -> Dict[str, Any]:
    """Assign or reassign a task to a person or organisation.

    Args:
        task_concept_id: The task's concept_id
        assignee_concept_id: The assignee's concept_id

    Returns:
        Updated task dict

    Raises:
        TaskNotFoundError: If task not found
    """
    normalised_task_id = ensure_v_concept_prefix(task_concept_id)
    normalised_assignee_id = ensure_v_concept_prefix(assignee_concept_id)
    if not normalised_task_id:
        raise TaskNotFoundError(f"Invalid task_concept_id: {task_concept_id}")
    if not normalised_assignee_id:
        raise InvalidTaskDataError(
            f"Invalid assignee_concept_id: {assignee_concept_id}"
        )
    task_concept_id = normalised_task_id
    assignee_concept_id = normalised_assignee_id

    # Verify task exists
    doc = ConceptsRepository.find_one({"concept_id": task_concept_id})
    if not doc:
        raise TaskNotFoundError(f"Task not found: {task_concept_id}")

    # Remove existing assignee(s) first, then add new one
    existing_assignees = doc.get("relationships", {}).get(PREDICATE_HAS_ASSIGNEE, [])
    for old_assignee in existing_assignees:
        ConceptsRepository.mutate_relationship_edge(
            source_id=task_concept_id,
            kind=PREDICATE_HAS_ASSIGNEE,
            target_id=old_assignee,
            action="remove",
        )

    ConceptsRepository.mutate_relationship_edge(
        source_id=task_concept_id,
        kind=PREDICATE_HAS_ASSIGNEE,
        target_id=assignee_concept_id,
        action="add",
    )

    # Update timestamp
    now = datetime.now(timezone.utc)
    ConceptsRepository.update_one(
        {"concept_id": task_concept_id},
        {"$set": {"updated_at": now}},
    )

    logger.info(f"Assigned task {task_concept_id} to {assignee_concept_id}")
    return get_task(task_concept_id)


def get_tasks_for_user(
    user_concept_id: str,
    *,
    status_filter: Optional[str] = None,
    include_created: bool = False,
) -> List[Dict[str, Any]]:
    """Get all tasks assigned to a user.

    Args:
        user_concept_id: The user's concept_id
        status_filter: Optional status to filter by
        include_created: If True, also include tasks created by the user

    Returns:
        List of task dicts
    """
    normalised_id = ensure_v_concept_prefix(user_concept_id)
    if not normalised_id:
        return []
    user_concept_id = normalised_id

    # Query for tasks assigned to this user
    query: Dict[str, Any] = {
        "relationships.is_an_instance_of": TASK_SPECIFICATION_TYPE_ID,
        f"relationships.{PREDICATE_HAS_ASSIGNEE}": user_concept_id,
    }

    if include_created:
        # Expand query to include tasks created by user
        query = {
            "relationships.is_an_instance_of": TASK_SPECIFICATION_TYPE_ID,
            "$or": [
                {f"relationships.{PREDICATE_HAS_ASSIGNEE}": user_concept_id},
                {f"relationships.{PREDICATE_HAS_CREATED_BY}": user_concept_id},
            ],
        }

    cursor = ConceptsRepository.find(query)
    tasks = [_build_task_response(doc) for doc in cursor]

    # Apply status filter if provided
    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]

    return tasks


def get_tasks_for_conversation(session_id: str) -> List[Dict[str, Any]]:
    """Get all tasks originating from a conversation.

    Args:
        session_id: The chat session_id

    Returns:
        List of task dicts
    """
    # First, find the conversation concept
    from .conversation_concept_service import get_conversation_concept_by_session_id

    conversation_concept_id = get_conversation_concept_by_session_id(session_id)
    if not conversation_concept_id:
        return []

    # Query for tasks linked to this conversation
    query = {
        "relationships.is_an_instance_of": TASK_SPECIFICATION_TYPE_ID,
        f"relationships.{PREDICATE_HAS_ORIGINATING_CONVERSATION}": conversation_concept_id,
    }

    cursor = ConceptsRepository.find(query)
    return [_build_task_response(doc) for doc in cursor]


def list_tasks(
    *,
    organisation_concept_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    priority_filter: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List tasks with optional filters.

    Args:
        organisation_concept_id: Filter by organisation
        status_filter: Filter by status
        priority_filter: Filter by priority
        limit: Maximum number of tasks to return

    Returns:
        List of task dicts
    """
    query: Dict[str, Any] = {
        "relationships.is_an_instance_of": TASK_SPECIFICATION_TYPE_ID,
    }

    if organisation_concept_id:
        organisation_concept_id = ensure_v_concept_prefix(organisation_concept_id)
        query["metadata.organisation_concept_id"] = organisation_concept_id

    cursor = ConceptsRepository.find(query, limit=limit)
    tasks = [_build_task_response(doc) for doc in cursor]

    # Apply status and priority filters (post-query since they're in text relations)
    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]
    if priority_filter:
        tasks = [t for t in tasks if t.get("priority") == priority_filter]

    return tasks


def delete_task(task_concept_id: str) -> bool:
    """Delete (archive) a task.

    Args:
        task_concept_id: The task's concept_id

    Returns:
        True if deleted

    Raises:
        TaskNotFoundError: If task not found
    """
    normalised_id = ensure_v_concept_prefix(task_concept_id)
    if not normalised_id:
        raise TaskNotFoundError(f"Invalid task_concept_id: {task_concept_id}")
    task_concept_id = normalised_id

    # Verify task exists
    doc = ConceptsRepository.find_one({"concept_id": task_concept_id})
    if not doc:
        raise TaskNotFoundError(f"Task not found: {task_concept_id}")

    # Soft delete by setting status to cancelled
    # (In future, could move to an archive collection)
    update_task_status(task_concept_id, TASK_STATUS_CANCELLED)

    logger.info(f"Deleted (cancelled) task: {task_concept_id}")
    return True


__all__ = [
    "TASK_SPECIFICATION_TYPE_ID",
    "TASK_EXECUTION_TYPE_ID",
    "CONVERSATION_TYPE_ID",
    "TASK_STATUS_PENDING",
    "TASK_STATUS_IN_PROGRESS",
    "TASK_STATUS_COMPLETED",
    "TASK_STATUS_CANCELLED",
    "TASK_STATUS_BLOCKED",
    "VALID_TASK_STATUSES",
    "PRIORITY_LOW",
    "PRIORITY_MEDIUM",
    "PRIORITY_HIGH",
    "PRIORITY_CRITICAL",
    "VALID_PRIORITIES",
    "TaskManagementError",
    "TaskNotFoundError",
    "InvalidTaskDataError",
    "create_task",
    "get_task",
    "update_task_status",
    "assign_task",
    "get_tasks_for_user",
    "get_tasks_for_conversation",
    "list_tasks",
    "delete_task",
]
