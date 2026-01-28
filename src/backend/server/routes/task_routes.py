"""Task management REST API routes for JVNAUTOSCI-1040.

Provides endpoints for creating, reading, updating, and deleting tasks.
Tasks are stored as Vontology concepts.
"""

from flask import Blueprint, request, jsonify, session
from flask.typing import ResponseReturnValue
import logging

from ...services.task_management_service import (
    create_task,
    get_task,
    update_task_status,
    assign_task,
    get_tasks_for_user,
    get_tasks_for_conversation,
    list_tasks,
    delete_task,
    TaskNotFoundError,
    InvalidTaskDataError,
    TaskManagementError,
)

logger = logging.getLogger(__name__)

task_bp = Blueprint("tasks", __name__)


def _get_current_user_concept_id() -> str | None:
    """Get the current user's concept_id using the same resolution as von_routes.

    Tries get_effective_user_concept_id first (window session aware),
    then falls back to Flask session.
    """
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if isinstance(user_concept_id, str) and user_concept_id.strip():
        return user_concept_id.strip()
    return None


def _get_current_org_concept_id() -> str | None:
    """Get the current organisation's concept_id from session."""
    org_concept_id = session.get("org_id") or session.get("organisation_concept_id")
    if isinstance(org_concept_id, str) and org_concept_id.strip():
        return org_concept_id.strip()
    return None


@task_bp.route("/", methods=["POST"])
def create_task_route() -> ResponseReturnValue:
    """Create a new task.

    Request body:
        title: str (required)
        description: str (required)
        assignee_concept_id: str (optional)
        due_date: str (optional, ISO 8601)
        priority: str (optional, default: medium)
        session_id: str (optional, link to conversation)
    """
    try:
        data = request.get_json() or {}

        title = data.get("title")
        description = data.get("description")

        if not title:
            return jsonify({"error": "title is required"}), 400
        if not description:
            return jsonify({"error": "description is required"}), 400

        # Get creator from session
        creator_concept_id = _get_current_user_concept_id()
        organisation_concept_id = _get_current_org_concept_id()

        result = create_task(
            title=title,
            description=description,
            assignee_concept_id=data.get("assignee_concept_id"),
            created_by_concept_id=creator_concept_id,
            originating_session_id=data.get("session_id"),
            due_date=data.get("due_date"),
            priority=data.get("priority", "medium"),
            organisation_concept_id=organisation_concept_id,
        )

        return jsonify(result), 201

    except InvalidTaskDataError as e:
        return jsonify({"error": str(e)}), 400
    except TaskManagementError as e:
        logger.error(f"Failed to create task: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Unexpected error creating task: {e}")
        return jsonify({"error": "Internal server error"}), 500


@task_bp.route("/<task_concept_id>", methods=["GET"])
def get_task_route(task_concept_id: str) -> ResponseReturnValue:
    """Get a task by concept_id."""
    try:
        result = get_task(task_concept_id)
        return jsonify(result), 200

    except TaskNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Unexpected error getting task: {e}")
        return jsonify({"error": "Internal server error"}), 500


@task_bp.route("/<task_concept_id>", methods=["PATCH"])
def update_task_route(task_concept_id: str) -> ResponseReturnValue:
    """Update a task.

    Request body (all fields optional):
        status: str (pending, in_progress, completed, cancelled, blocked)
        assignee_concept_id: str
    """
    try:
        data = request.get_json() or {}

        # Handle status update
        if "status" in data:
            result = update_task_status(task_concept_id, data["status"])
        # Handle assignee update
        elif "assignee_concept_id" in data:
            result = assign_task(task_concept_id, data["assignee_concept_id"])
        else:
            return jsonify({"error": "No update fields provided"}), 400

        return jsonify(result), 200

    except TaskNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except InvalidTaskDataError as e:
        return jsonify({"error": str(e)}), 400
    except TaskManagementError as e:
        logger.error(f"Failed to update task: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Unexpected error updating task: {e}")
        return jsonify({"error": "Internal server error"}), 500


@task_bp.route("/<task_concept_id>", methods=["DELETE"])
def delete_task_route(task_concept_id: str) -> ResponseReturnValue:
    """Delete (cancel) a task."""
    try:
        delete_task(task_concept_id)
        return jsonify({"success": True}), 200

    except TaskNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except TaskManagementError as e:
        logger.error(f"Failed to delete task: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Unexpected error deleting task: {e}")
        return jsonify({"error": "Internal server error"}), 500


@task_bp.route("/", methods=["GET"])
def list_tasks_route() -> ResponseReturnValue:
    """List tasks, optionally filtered.

    Query parameters:
        user: str - Filter by user concept_id (assigned or created)
        status: str - Filter by status
        session_id: str - Filter by originating conversation
        priority: str - Filter by priority
        include_created: bool - Include tasks created by user (not just assigned)
    """
    try:
        user_concept_id = request.args.get("user")
        status_filter = request.args.get("status")
        session_id = request.args.get("session_id")
        priority_filter = request.args.get("priority")
        include_created = request.args.get("include_created", "false").lower() == "true"

        # If filtering by user, use get_tasks_for_user
        if user_concept_id:
            tasks = get_tasks_for_user(
                user_concept_id=user_concept_id,
                status_filter=status_filter,
                include_created=include_created,
            )
        # If filtering by session, use get_tasks_for_conversation
        elif session_id:
            tasks = get_tasks_for_conversation(session_id=session_id)
        else:
            # General list with optional filters
            tasks = list_tasks(
                status_filter=status_filter,
                priority_filter=priority_filter,
            )

        return jsonify({"tasks": tasks, "count": len(tasks)}), 200

    except Exception as e:
        logger.error(f"Unexpected error listing tasks: {e}")
        return jsonify({"error": "Internal server error"}), 500


@task_bp.route("/my", methods=["GET"])
def my_tasks_route() -> ResponseReturnValue:
    """List tasks assigned to the current user.

    Query parameters:
        status: str - Filter by status
        include_created: bool - Include tasks created by user
    """
    try:
        user_concept_id = _get_current_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        status_filter = request.args.get("status")
        include_created = request.args.get("include_created", "false").lower() == "true"

        tasks = get_tasks_for_user(
            user_concept_id=user_concept_id,
            status_filter=status_filter,
            include_created=include_created,
        )

        return jsonify({"tasks": tasks, "count": len(tasks)}), 200

    except Exception as e:
        logger.error(f"Unexpected error listing my tasks: {e}")
        return jsonify({"error": "Internal server error"}), 500
