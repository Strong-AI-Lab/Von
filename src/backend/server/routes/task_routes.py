"""Task management REST API routes for JVNAUTOSCI-1040.

Provides endpoints for creating, reading, updating, and deleting tasks.
Tasks are stored as Vontology concepts.
"""

from datetime import datetime, timezone
import logging

from flask import Blueprint, request, jsonify, session
from flask.typing import ResponseReturnValue

from ...services.task_management_service import (
    create_task,
    get_task,
    update_task_fields,
    get_tasks_for_user,
    get_tasks_for_conversation,
    list_tasks,
    search_tasks,
    delete_task,
    add_task_comment,
    list_task_comments,
    add_task_attachment,
    list_task_attachments,
    get_task_history,
    link_tasks,
    unlink_tasks,
    TaskNotFoundError,
    InvalidTaskDataError,
    TaskManagementError,
)

logger = logging.getLogger(__name__)

task_bp = Blueprint("tasks", __name__)


def _parse_optional_datetime(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        raise InvalidTaskDataError(f"{field_name} must be an ISO 8601 datetime string")
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidTaskDataError(
            f"{field_name} must be an ISO 8601 datetime string"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_optional_bool(raw_value: str | None, field_name: str) -> bool | None:
    if raw_value is None:
        return None
    lowered = raw_value.strip().lower()
    if not lowered:
        return None
    if lowered in {"true", "1", "yes", "y"}:
        return True
    if lowered in {"false", "0", "no", "n"}:
        return False
    raise InvalidTaskDataError(f"{field_name} must be a boolean")


def _parse_csv_param(raw_value: str | None) -> list[str] | None:
    if raw_value is None:
        return None
    values = [item.strip() for item in raw_value.split(",") if item.strip()]
    return values or None


def _parse_int_param(raw_value: str | None, default: int) -> int:
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return int(raw_value.strip())
    except ValueError:
        raise InvalidTaskDataError(
            f"Expected integer query parameter, received: {raw_value}"
        )


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
        start_date: str (optional, ISO 8601)
        due_date: str (optional, ISO 8601)
        epic_task_concept_id: str (optional)
        components: list[str] (optional)
        fix_versions: list[str] (optional)
        sprint_values: list[str] (optional)
        backlog_rank: str (optional)
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
        parsed_start_date = _parse_optional_datetime(data.get("start_date"), "start_date")
        parsed_due_date = _parse_optional_datetime(data.get("due_date"), "due_date")

        result = create_task(
            title=title,
            description=description,
            assignee_concept_id=data.get("assignee_concept_id"),
            created_by_concept_id=creator_concept_id,
            originating_session_id=data.get("session_id"),
            start_date=parsed_start_date,
            due_date=parsed_due_date,
            epic_task_concept_id=data.get("epic_task_concept_id"),
            components=data.get("components"),
            fix_versions=data.get("fix_versions"),
            sprint_values=data.get("sprint_values"),
            backlog_rank=data.get("backlog_rank"),
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
        title: str
        description: str
        priority: str
        start_date: str (ISO 8601)
        due_date: str (ISO 8601)
        labels: list[str]
        components: list[str]
        fix_versions: list[str]
        sprint_values: list[str]
        backlog_rank: str
        parent_task_concept_id: str
        epic_task_concept_id: str
    """
    try:
        data = request.get_json() or {}
        if not data:
            return jsonify({"error": "No update fields provided"}), 400

        actor_concept_id = _get_current_user_concept_id()
        result = update_task_fields(
            task_concept_id,
            fields=data,
            actor_concept_id=actor_concept_id,
        )

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


@task_bp.route("/search", methods=["GET"])
def search_tasks_route() -> ResponseReturnValue:
    """Search tasks with rich filters."""
    try:
        statuses = request.args.getlist("status")
        if not statuses:
            statuses = request.args.getlist("statuses")
        if not statuses:
            statuses = _parse_csv_param(request.args.get("statuses")) or []

        labels = request.args.getlist("label")
        if not labels:
            labels = request.args.getlist("labels")
        if not labels:
            labels = _parse_csv_param(request.args.get("labels")) or []

        components = request.args.getlist("component")
        if not components:
            components = request.args.getlist("components")
        if not components:
            components = _parse_csv_param(request.args.get("components")) or []

        fix_versions = request.args.getlist("fix_version")
        if not fix_versions:
            fix_versions = request.args.getlist("fix_versions")
        if not fix_versions:
            fix_versions = _parse_csv_param(request.args.get("fix_versions")) or []

        sprint_values = request.args.getlist("sprint")
        if not sprint_values:
            sprint_values = request.args.getlist("sprints")
        if not sprint_values:
            sprint_values = _parse_csv_param(request.args.get("sprints")) or []

        result = search_tasks(
            query=request.args.get("query"),
            status_filter=request.args.get("status_filter"),
            statuses=statuses or None,
            assignee_concept_id=request.args.get("assignee_concept_id")
            or request.args.get("assignee_id")
            or request.args.get("user_concept_id"),
            labels=labels or None,
            components=components or None,
            fix_versions=fix_versions or None,
            sprint_values=sprint_values or None,
            backlog_rank=request.args.get("backlog_rank"),
            parent_task_concept_id=request.args.get("parent_task_concept_id"),
            epic_task_concept_id=request.args.get("epic_task_concept_id"),
            has_parent=_parse_optional_bool(request.args.get("has_parent"), "has_parent"),
            has_subtasks=_parse_optional_bool(
                request.args.get("has_subtasks"), "has_subtasks"
            ),
            has_epic=_parse_optional_bool(request.args.get("has_epic"), "has_epic"),
            has_backlog_rank=_parse_optional_bool(
                request.args.get("has_backlog_rank"),
                "has_backlog_rank",
            ),
            start_from=request.args.get("start_from"),
            start_to=request.args.get("start_to"),
            due_from=request.args.get("due_from"),
            due_to=request.args.get("due_to"),
            created_from=request.args.get("created_from"),
            created_to=request.args.get("created_to"),
            updated_from=request.args.get("updated_from"),
            updated_to=request.args.get("updated_to"),
            dependency_state=request.args.get("dependency_state"),
            organisation_concept_id=request.args.get("organisation_concept_id")
            or _get_current_org_concept_id(),
            limit=_parse_int_param(request.args.get("limit"), 50),
            offset=_parse_int_param(request.args.get("offset"), 0),
        )
        return jsonify(result), 200
    except InvalidTaskDataError as e:
        return jsonify({"error": str(e)}), 400
    except TaskManagementError as e:
        logger.error(f"Failed to search tasks: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Unexpected error searching tasks: {e}")
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


@task_bp.route("/<task_concept_id>/comments", methods=["GET"])
def list_task_comments_route(task_concept_id: str) -> ResponseReturnValue:
    """List comments for a task."""
    try:
        limit = _parse_int_param(request.args.get("limit"), 100)
        offset = _parse_int_param(request.args.get("offset"), 0)
        result = list_task_comments(
            task_concept_id,
            limit=limit,
            offset=offset,
        )
        return jsonify(result), 200
    except TaskNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except InvalidTaskDataError as e:
        return jsonify({"error": str(e)}), 400
    except TaskManagementError as e:
        logger.error(f"Failed to list task comments: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Unexpected error listing task comments: {e}")
        return jsonify({"error": "Internal server error"}), 500


@task_bp.route("/<task_concept_id>/comments", methods=["POST"])
def add_task_comment_route(task_concept_id: str) -> ResponseReturnValue:
    """Add a comment to a task."""
    try:
        data = request.get_json() or {}
        body = data.get("body")
        if not isinstance(body, str) or not body.strip():
            return jsonify({"error": "body is required"}), 400

        author_concept_id = _get_current_user_concept_id()
        comment = add_task_comment(
            task_concept_id,
            body=body,
            author_concept_id=author_concept_id,
        )
        return jsonify(comment), 201
    except TaskNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except InvalidTaskDataError as e:
        return jsonify({"error": str(e)}), 400
    except TaskManagementError as e:
        logger.error(f"Failed to add task comment: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Unexpected error adding task comment: {e}")
        return jsonify({"error": "Internal server error"}), 500


@task_bp.route("/<task_concept_id>/attachments", methods=["GET"])
def list_task_attachments_route(task_concept_id: str) -> ResponseReturnValue:
    """List attachments for a task."""
    try:
        limit = _parse_int_param(request.args.get("limit"), 100)
        offset = _parse_int_param(request.args.get("offset"), 0)
        result = list_task_attachments(
            task_concept_id,
            limit=limit,
            offset=offset,
        )
        return jsonify(result), 200
    except TaskNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except InvalidTaskDataError as e:
        return jsonify({"error": str(e)}), 400
    except TaskManagementError as e:
        logger.error(f"Failed to list task attachments: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Unexpected error listing task attachments: {e}")
        return jsonify({"error": "Internal server error"}), 500


@task_bp.route("/<task_concept_id>/attachments", methods=["POST"])
def add_task_attachment_route(task_concept_id: str) -> ResponseReturnValue:
    """Add an attachment record to a task."""
    try:
        data = request.get_json() or {}
        filename = data.get("filename")
        uri = data.get("uri")
        if not isinstance(filename, str) or not filename.strip():
            return jsonify({"error": "filename is required"}), 400
        if not isinstance(uri, str) or not uri.strip():
            return jsonify({"error": "uri is required"}), 400

        raw_size = data.get("size_bytes")
        size_bytes = None
        if raw_size is not None:
            try:
                size_bytes = int(raw_size)
            except (TypeError, ValueError):
                raise InvalidTaskDataError("size_bytes must be an integer")

        attachment = add_task_attachment(
            task_concept_id,
            filename=filename,
            uri=uri,
            media_type=data.get("media_type"),
            size_bytes=size_bytes,
            added_by_concept_id=_get_current_user_concept_id(),
            note=data.get("note"),
        )
        return jsonify(attachment), 201
    except TaskNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except InvalidTaskDataError as e:
        return jsonify({"error": str(e)}), 400
    except TaskManagementError as e:
        logger.error(f"Failed to add task attachment: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Unexpected error adding task attachment: {e}")
        return jsonify({"error": "Internal server error"}), 500


@task_bp.route("/<task_concept_id>/history", methods=["GET"])
def get_task_history_route(task_concept_id: str) -> ResponseReturnValue:
    """List task history events."""
    try:
        limit = _parse_int_param(request.args.get("limit"), 200)
        offset = _parse_int_param(request.args.get("offset"), 0)
        result = get_task_history(
            task_concept_id,
            limit=limit,
            offset=offset,
        )
        return jsonify(result), 200
    except TaskNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except InvalidTaskDataError as e:
        return jsonify({"error": str(e)}), 400
    except TaskManagementError as e:
        logger.error(f"Failed to get task history: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Unexpected error getting task history: {e}")
        return jsonify({"error": "Internal server error"}), 500


@task_bp.route("/<task_concept_id>/links", methods=["POST"])
def add_task_link_route(task_concept_id: str) -> ResponseReturnValue:
    """Add a typed link between tasks."""
    try:
        data = request.get_json() or {}
        target_task_concept_id = data.get("target_task_concept_id")
        link_type = data.get("link_type")
        if not isinstance(target_task_concept_id, str) or not target_task_concept_id.strip():
            return jsonify({"error": "target_task_concept_id is required"}), 400
        if not isinstance(link_type, str) or not link_type.strip():
            return jsonify({"error": "link_type is required"}), 400

        result = link_tasks(
            task_concept_id,
            target_task_concept_id,
            link_type=link_type,
            actor_concept_id=_get_current_user_concept_id(),
        )
        return jsonify(result), 201
    except TaskNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except InvalidTaskDataError as e:
        return jsonify({"error": str(e)}), 400
    except TaskManagementError as e:
        logger.error(f"Failed to add task link: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Unexpected error adding task link: {e}")
        return jsonify({"error": "Internal server error"}), 500


@task_bp.route("/<task_concept_id>/links", methods=["DELETE"])
def remove_task_link_route(task_concept_id: str) -> ResponseReturnValue:
    """Remove a typed link between tasks."""
    try:
        data = request.get_json() or {}
        target_task_concept_id = data.get("target_task_concept_id")
        link_type = data.get("link_type")
        if not isinstance(target_task_concept_id, str) or not target_task_concept_id.strip():
            return jsonify({"error": "target_task_concept_id is required"}), 400
        if not isinstance(link_type, str) or not link_type.strip():
            return jsonify({"error": "link_type is required"}), 400

        result = unlink_tasks(
            task_concept_id,
            target_task_concept_id,
            link_type=link_type,
            actor_concept_id=_get_current_user_concept_id(),
        )
        return jsonify(result), 200
    except TaskNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except InvalidTaskDataError as e:
        return jsonify({"error": str(e)}), 400
    except TaskManagementError as e:
        logger.error(f"Failed to remove task link: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error(f"Unexpected error removing task link: {e}")
        return jsonify({"error": "Internal server error"}), 500


@task_bp.route("/<task_concept_id>/links/remove", methods=["POST"])
def remove_task_link_post_route(task_concept_id: str) -> ResponseReturnValue:
    """Remove a typed link between tasks via POST payload."""
    return remove_task_link_route(task_concept_id)
