from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Blueprint, jsonify, request

from ...db.repositories.concepts_repository import ConceptsRepository
from ...services.text_value_service import get_texts_for_concept
from ...services.workflow_episode_service import (
    get_workflow_usage_aggregates_for_workflows,
    list_workflow_use_episodes,
)
from ...workflows.trace_store import (
    get_workflow_execution_trace,
    list_recent_workflow_execution_traces,
)
from ...workflows.durable import (
    WorkflowInstance,
    WorkflowInstanceStatus,
    WorkflowSchedule,
    ScheduleType,
    WorkflowInstanceManager,
)
from ...workflows.durable.workflow_instance_submission_service import (
    submit_verified_workflow_instance,
)
from ...workflows.vontology_loader import (
    build_workflow_process_graph,
    best_effort_workflow_narrative_text,
)

logger = logging.getLogger(__name__)

workflows_bp = Blueprint("workflows", __name__)


@workflows_bp.get("/api/workflows/definitions/<path:workflow_id>")
def api_get_workflow_definition(workflow_id: str):
    definition, warnings = build_workflow_process_graph(workflow_id)
    raw = best_effort_workflow_narrative_text(workflow_id)

    if not definition:
        # Backward compatibility: if a workflow only has narrative text, return it.
        # The preferred representation is explicit step/control-flow relationships.
        if raw:
            return jsonify(
                {
                    "workflow_id": workflow_id,
                    "definition": {"representation": "narrative_only"},
                    "raw": raw,
                    "warnings": warnings,
                }
            )

        return (
            jsonify(
                {"error": "workflow_definition_not_found", "workflow_id": workflow_id}
            ),
            404,
        )

    return jsonify(
        {
            "workflow_id": workflow_id,
            "definition": definition,
            "raw": raw,
            "warnings": warnings,
        }
    )


@workflows_bp.get("/api/workflows/definitions")
def api_list_workflow_definitions():
    """List workflow definitions visible to the workflow engine registry."""

    limit_raw = request.args.get("limit", "200")
    try:
        limit = int(limit_raw)
    except Exception:
        limit = 200
    limit = max(1, min(limit, 500))

    try:
        from ...workflows.durable.registry_factory import (
            build_durable_workflow_registry_read_only,
        )

        # Read-only build avoids concept bootstrap writes on list/introspection paths.
        registry = build_durable_workflow_registry_read_only()
        workflow_ids = sorted(list(registry.all_workflow_ids()))
        selected_ids = workflow_ids[:limit]
        usage_aggregate_map = get_workflow_usage_aggregates_for_workflows(selected_ids)

        items: List[Dict[str, Any]] = []
        for workflow_id in selected_ids:
            registration = registry.get_registration(workflow_id)
            definition = (
                registration.definition
                if registration is not None
                else registry.get(workflow_id)
            )

            description = ""
            source = "unknown"
            if registration is not None:
                if (
                    isinstance(registration.purpose, str)
                    and registration.purpose.strip()
                ):
                    description = registration.purpose.strip()
                if isinstance(registration.source, str) and registration.source.strip():
                    source = registration.source.strip()
            if not description and definition is not None:
                purpose = getattr(definition, "purpose", "")
                if isinstance(purpose, str) and purpose.strip():
                    description = purpose.strip()

            initial_state = ""
            if definition is not None:
                state_value = getattr(definition, "initial_state", "")
                if isinstance(state_value, str):
                    initial_state = state_value

            usage = (
                usage_aggregate_map.get(workflow_id, {})
                if isinstance(usage_aggregate_map, dict)
                else {}
            )
            attempts = usage.get("attempts")
            completions = usage.get("completions")
            completion_rate = usage.get("completion_rate")

            items.append(
                {
                    "workflow_id": workflow_id,
                    "description": description,
                    "initial_state": initial_state,
                    "source": source,
                    "attempts": (
                        int(attempts) if isinstance(attempts, (int, float)) else 0
                    ),
                    "completions": (
                        int(completions)
                        if isinstance(completions, (int, float))
                        else 0
                    ),
                    "completion_rate": (
                        float(completion_rate)
                        if isinstance(completion_rate, (int, float))
                        else None
                    ),
                    "last_episode_at": (
                        str(usage.get("last_episode_at"))
                        if usage.get("last_episode_at") is not None
                        else None
                    ),
                }
            )

        return jsonify(
            {
                "items": items,
                "count": len(items),
                "total": len(workflow_ids),
            }
        )
    except Exception as exc:
        logger.exception("Failed to list workflow definitions via API")
        return jsonify({"error": "workflow_definitions_list_failed", "detail": str(exc)}), 500


@workflows_bp.get("/api/workflows/executions/<execution_id>")
def api_get_workflow_execution(execution_id: str):
    doc = get_workflow_execution_trace(execution_id)
    if not doc:
        return (
            jsonify(
                {"error": "workflow_execution_not_found", "execution_id": execution_id}
            ),
            404,
        )
    return jsonify(doc)


@workflows_bp.get("/api/workflows/executions/recent")
def api_list_recent_workflow_executions():
    limit_raw = request.args.get("limit", "20")
    try:
        limit = int(limit_raw)
    except Exception:
        limit = 20
    namespace = request.args.get("namespace")

    docs = list_recent_workflow_execution_traces(
        limit=limit, namespace=namespace or None
    )
    return jsonify({"items": docs, "count": len(docs)})


@workflows_bp.get("/api/workflows/episodes")
def api_list_workflow_use_episodes():
    """List workflow-use episodes for monitor drill-down diagnostics."""

    workflow_id = request.args.get("workflow_id")
    namespace = request.args.get("namespace")
    session_id = request.args.get("session_id")
    turn_id = request.args.get("turn_id")
    try:
        limit = int(request.args.get("limit", "50"))
    except Exception:
        limit = 50
    limit = max(1, min(limit, 200))

    items = list_workflow_use_episodes(
        workflow_id=workflow_id or None,
        namespace=namespace or None,
        session_id=session_id or None,
        turn_id=turn_id or None,
        limit=limit,
    )
    return jsonify(
        {
            "items": items,
            "count": len(items),
            "filters": {
                "workflow_id": workflow_id or None,
                "namespace": namespace or None,
                "session_id": session_id or None,
                "turn_id": turn_id or None,
            },
        }
    )


# =============================================================================
# Durable Workflow Instance Endpoints (JVNAUTOSCI-1075)
# =============================================================================

# Singleton manager instance for API use
_instance_manager: WorkflowInstanceManager | None = None


def _get_instance_manager() -> WorkflowInstanceManager:
    """Get or create the singleton WorkflowInstanceManager."""
    global _instance_manager
    if _instance_manager is None:
        _instance_manager = WorkflowInstanceManager()
    return _instance_manager


@workflows_bp.post("/api/workflows/instances")
def api_create_workflow_instance():
    """Create a new durable workflow instance.

    Request body:
    {
        "workflow_id": "#V#example_workflow",
        "user_id": "user-123",
        "org_id": "org-456",
        "namespace": "user-123/org-456",
        "inputs": {"param1": "value1"},
        "max_retries": 3
    }
    """
    data = request.get_json() or {}

    workflow_id = data.get("workflow_id")
    if not workflow_id:
        return jsonify({"error": "workflow_id is required"}), 400

    user_id = data.get("user_id", "anonymous")
    org_id = data.get("org_id", "default")
    namespace = data.get("namespace", f"{user_id}/{org_id}")
    inputs = data.get("inputs", {})
    max_retries = data.get("max_retries", 3)

    try:
        manager = _get_instance_manager()
        submission = submit_verified_workflow_instance(
            manager=manager,
            workflow_id=workflow_id,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            inputs=inputs,
            max_retries=max_retries,
        )
        payload = submission.to_dict()
        status_code = 201 if submission.success else 400
        return jsonify(payload), status_code
    except Exception as e:
        logger.exception("Failed to create workflow instance")
        return jsonify({"error": str(e)}), 500


@workflows_bp.get("/api/workflows/instances")
def api_list_workflow_instances():
    """List workflow instances with optional filters.

    Query params:
    - user_id: Filter by user
    - org_id: Filter by organisation
    - namespace: Filter by namespace
    - status: Filter by status (pending, running, completed, failed, cancelled, paused)
    - workflow_id: Filter by workflow definition
    - source_event_type: Filter by triggering event type
    - source_event_id: Filter by triggering event ID
    - limit: Maximum results (default 50)
    """
    user_id = request.args.get("user_id")
    org_id = request.args.get("org_id")
    namespace = request.args.get("namespace")
    status_str = request.args.get("status")
    workflow_id = request.args.get("workflow_id")
    source_event_type = request.args.get("source_event_type")
    source_event_id = request.args.get("source_event_id")
    limit = min(int(request.args.get("limit", "50")), 200)

    status: WorkflowInstanceStatus | None = None
    if status_str:
        try:
            status = WorkflowInstanceStatus(status_str.lower())
        except ValueError:
            return jsonify({"error": f"Invalid status: {status_str}"}), 400

    manager = _get_instance_manager()
    instances = manager.list_instances(
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
        status=status,
        workflow_id=workflow_id,
        source_event_type=source_event_type,
        source_event_id=source_event_id,
        limit=limit,
    )

    return jsonify(
        {
            "items": [inst.to_status_dict() for inst in instances],
            "count": len(instances),
        }
    )


@workflows_bp.get("/api/workflows/instances/<instance_id>")
def api_get_workflow_instance(instance_id: str):
    """Get details of a specific workflow instance."""
    manager = _get_instance_manager()
    instance = manager.get_instance(instance_id)

    if not instance:
        return (
            jsonify(
                {
                    "error": "instance_not_found",
                    "instance_id": instance_id,
                }
            ),
            404,
        )

    # Return full details including inputs/outputs
    result = instance.to_status_dict()
    result["inputs"] = instance.inputs
    result["outputs"] = instance.outputs
    result["user_id"] = instance.user_id
    result["org_id"] = instance.org_id
    result["namespace"] = instance.namespace
    result["error_step"] = instance.error_step
    result["schedule_id"] = instance.schedule_id
    result["workflow_data"] = instance.workflow_data

    return jsonify(result)


@workflows_bp.post("/api/workflows/instances/<instance_id>/cancel")
def api_cancel_workflow_instance(instance_id: str):
    """Cancel a running or pending workflow instance."""
    manager = _get_instance_manager()

    # Verify instance exists
    instance = manager.get_instance(instance_id)
    if not instance:
        return (
            jsonify(
                {
                    "error": "instance_not_found",
                    "instance_id": instance_id,
                }
            ),
            404,
        )

    if instance.status.is_terminal():
        return (
            jsonify(
                {
                    "error": "instance_already_terminal",
                    "status": instance.status.value,
                }
            ),
            400,
        )

    success = manager.mark_cancelled(instance_id)
    if success:
        return jsonify({"status": "cancelled", "instance_id": instance_id})
    else:
        return jsonify({"error": "cancel_failed"}), 500


@workflows_bp.post("/api/workflows/instances/<instance_id>/retry")
def api_retry_workflow_instance(instance_id: str):
    """Reset a failed workflow instance for retry."""
    manager = _get_instance_manager()

    # Verify instance exists
    instance = manager.get_instance(instance_id)
    if not instance:
        return (
            jsonify(
                {
                    "error": "instance_not_found",
                    "instance_id": instance_id,
                }
            ),
            404,
        )

    if instance.status != WorkflowInstanceStatus.FAILED:
        return (
            jsonify(
                {
                    "error": "instance_not_failed",
                    "status": instance.status.value,
                }
            ),
            400,
        )

    success = manager.reset_for_retry(instance_id)
    if success:
        return jsonify(
            {
                "status": "pending",
                "instance_id": instance_id,
                "retry_count": instance.retry_count,
            }
        )
    else:
        return (
            jsonify(
                {
                    "error": "retry_limit_exceeded",
                    "retry_count": instance.retry_count,
                    "max_retries": instance.max_retries,
                }
            ),
            400,
        )


@workflows_bp.post("/api/workflows/instances/<instance_id>/pause")
def api_pause_workflow_instance(instance_id: str):
    """Pause a running workflow instance."""
    manager = _get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        return (
            jsonify(
                {
                    "error": "instance_not_found",
                    "instance_id": instance_id,
                }
            ),
            404,
        )

    if instance.status != WorkflowInstanceStatus.RUNNING:
        return (
            jsonify(
                {
                    "error": "instance_not_running",
                    "status": instance.status.value,
                }
            ),
            400,
        )

    success = manager.pause_instance(instance_id)
    if success:
        return jsonify({"status": "paused", "instance_id": instance_id})
    else:
        return jsonify({"error": "pause_failed"}), 500


@workflows_bp.get("/api/workflows/instances/stream")
def api_stream_workflow_instances():
    """Stream workflow status updates via SSE.

    Query params:
    - user_id: Filter by user
    - org_id: Filter by organisation
    - namespace: Filter by namespace
    - workflow_id: Filter by workflow definition
    - instance_id: Filter to a single instance
    - status: Comma-separated list of statuses
    """
    from flask import Response, stream_with_context

    try:
        from ...services.durable_workflow_stream_service import (
            get_workflow_stream_service,
        )

        user_id = request.args.get("user_id")
        org_id = request.args.get("org_id")
        namespace = request.args.get("namespace")
        workflow_id = request.args.get("workflow_id")
        instance_id = request.args.get("instance_id")
        status_raw = request.args.get("status")

        statuses = None
        if isinstance(status_raw, str) and status_raw.strip():
            statuses = {
                status.strip().lower()
                for status in status_raw.split(",")
                if status.strip()
            }

        stream_service = get_workflow_stream_service()
        subscriber = stream_service.subscribe(
            user_id=user_id or None,
            org_id=org_id or None,
            namespace=namespace or None,
            workflow_id=workflow_id or None,
            instance_id=instance_id or None,
            statuses=statuses,
        )

        def generate():
            try:
                for event_data in stream_service.generate_events(subscriber):
                    yield event_data
            finally:
                stream_service.unsubscribe(subscriber)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    except Exception as e:
        logger.exception("Workflow status stream error")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Durable Workflow Schedule Endpoints
# =============================================================================


@workflows_bp.post("/api/workflows/schedules")
def api_create_workflow_schedule():
    """Create a new workflow schedule.

    Request body:
    {
        "workflow_id": "#V#example_workflow",
        "user_id": "user-123",
        "org_id": "org-456",
        "namespace": "user-123/org-456",
        "schedule_type": "interval" | "cron" | "once",
        "interval_seconds": 3600,  // for interval type
        "cron_expression": "0 9 * * 1-5",  // for cron type
        "run_at": "2026-02-04T10:00:00Z",  // for once type
        "default_inputs": {"param1": "value1"},
        "description": "Daily sync"
    }
    """
    data = request.get_json() or {}

    workflow_id = data.get("workflow_id")
    if not workflow_id:
        return jsonify({"error": "workflow_id is required"}), 400

    user_id = data.get("user_id", "anonymous")
    org_id = data.get("org_id", "default")
    namespace = data.get("namespace", f"{user_id}/{org_id}")
    default_inputs = data.get("default_inputs", {})
    description = data.get("description")

    schedule_type_str = data.get("schedule_type", "interval")
    try:
        schedule_type = ScheduleType(schedule_type_str.lower())
    except ValueError:
        return jsonify({"error": f"Invalid schedule_type: {schedule_type_str}"}), 400

    schedule: WorkflowSchedule | None = None

    if schedule_type == ScheduleType.INTERVAL:
        interval_seconds = data.get("interval_seconds")
        if not interval_seconds or not isinstance(interval_seconds, int):
            return (
                jsonify({"error": "interval_seconds is required for interval type"}),
                400,
            )
        schedule = WorkflowSchedule.create_interval(
            workflow_id,
            interval_seconds=interval_seconds,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            default_inputs=default_inputs,
            description=description,
        )

    elif schedule_type == ScheduleType.CRON:
        cron_expression = data.get("cron_expression")
        if not cron_expression:
            return jsonify({"error": "cron_expression is required for cron type"}), 400
        schedule = WorkflowSchedule.create_cron(
            workflow_id,
            cron_expression=cron_expression,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            default_inputs=default_inputs,
            description=description,
        )

    elif schedule_type == ScheduleType.ONCE:
        run_at_str = data.get("run_at")
        if not run_at_str:
            return jsonify({"error": "run_at is required for once type"}), 400
        try:
            run_at = datetime.fromisoformat(run_at_str.replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"error": "Invalid run_at datetime format"}), 400
        schedule = WorkflowSchedule.create_once(
            workflow_id,
            run_at=run_at,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            default_inputs=default_inputs,
            description=description,
        )

    if schedule is None:
        return jsonify({"error": "Failed to create schedule"}), 500

    try:
        manager = _get_instance_manager()
        schedule_id = manager.create_schedule(schedule)
        return (
            jsonify(
                {
                    "schedule_id": schedule_id,
                    "schedule_type": schedule_type.value,
                }
            ),
            201,
        )
    except Exception as e:
        logger.exception("Failed to create workflow schedule")
        return jsonify({"error": str(e)}), 500


@workflows_bp.get("/api/workflows/schedules")
def api_list_workflow_schedules():
    """List workflow schedules with optional filters.

    Query params:
    - user_id: Filter by user
    - enabled_only: Only return enabled schedules (default false)
    - limit: Maximum results (default 50)
    """
    user_id = request.args.get("user_id")
    enabled_only = request.args.get("enabled_only", "false").lower() == "true"
    limit = min(int(request.args.get("limit", "50")), 200)

    manager = _get_instance_manager()
    schedules = manager.list_schedules(
        user_id=user_id,
        enabled_only=enabled_only,
        limit=limit,
    )

    return jsonify(
        {
            "items": [sched.to_status_dict() for sched in schedules],
            "count": len(schedules),
        }
    )


@workflows_bp.get("/api/workflows/schedules/<schedule_id>")
def api_get_workflow_schedule(schedule_id: str):
    """Get details of a specific workflow schedule."""
    manager = _get_instance_manager()
    schedule = manager.get_schedule(schedule_id)

    if not schedule:
        return (
            jsonify(
                {
                    "error": "schedule_not_found",
                    "schedule_id": schedule_id,
                }
            ),
            404,
        )

    result = schedule.to_status_dict()
    result["user_id"] = schedule.user_id
    result["org_id"] = schedule.org_id
    result["namespace"] = schedule.namespace
    result["default_inputs"] = schedule.default_inputs
    result["created_at"] = (
        schedule.created_at.isoformat() if schedule.created_at else None
    )
    result["updated_at"] = (
        schedule.updated_at.isoformat() if schedule.updated_at else None
    )

    return jsonify(result)


@workflows_bp.put("/api/workflows/schedules/<schedule_id>/enabled")
def api_set_schedule_enabled(schedule_id: str):
    """Enable or disable a workflow schedule.

    Request body:
    {
        "enabled": true | false
    }
    """
    data = request.get_json() or {}
    enabled = data.get("enabled")

    if enabled is None or not isinstance(enabled, bool):
        return jsonify({"error": "enabled (boolean) is required"}), 400

    manager = _get_instance_manager()

    # Verify schedule exists
    schedule = manager.get_schedule(schedule_id)
    if not schedule:
        return (
            jsonify(
                {
                    "error": "schedule_not_found",
                    "schedule_id": schedule_id,
                }
            ),
            404,
        )

    success = manager.set_schedule_enabled(schedule_id, enabled)
    if success:
        return jsonify(
            {
                "schedule_id": schedule_id,
                "enabled": enabled,
            }
        )
    else:
        return jsonify({"error": "update_failed"}), 500


@workflows_bp.delete("/api/workflows/schedules/<schedule_id>")
def api_delete_workflow_schedule(schedule_id: str):
    """Delete a workflow schedule."""
    manager = _get_instance_manager()

    # Verify schedule exists
    schedule = manager.get_schedule(schedule_id)
    if not schedule:
        return (
            jsonify(
                {
                    "error": "schedule_not_found",
                    "schedule_id": schedule_id,
                }
            ),
            404,
        )

    success = manager.delete_schedule(schedule_id)
    if success:
        return jsonify({"deleted": True, "schedule_id": schedule_id})
    else:
        return jsonify({"error": "delete_failed"}), 500


@workflows_bp.post("/api/workflows/schedules/<schedule_id>/trigger")
def api_trigger_workflow_schedule(schedule_id: str):
    """Manually trigger a workflow schedule immediately.

    Creates a new workflow instance from the schedule's configuration.
    """
    manager = _get_instance_manager()

    schedule = manager.get_schedule(schedule_id)
    if not schedule:
        return (
            jsonify(
                {
                    "error": "schedule_not_found",
                    "schedule_id": schedule_id,
                }
            ),
            404,
        )

    try:
        submission = submit_verified_workflow_instance(
            manager=manager,
            workflow_id=schedule.workflow_id,
            user_id=schedule.user_id,
            org_id=schedule.org_id,
            namespace=schedule.namespace,
            inputs=schedule.default_inputs,
            schedule_id=schedule.schedule_id,
        )
        payload = submission.to_dict()
        payload["schedule_id"] = schedule_id
        if not submission.success:
            return jsonify(payload), 400
        payload["status"] = "triggered"
        return (
            jsonify(payload),
            201,
        )
    except Exception as e:
        logger.exception("Failed to trigger workflow schedule")
        return jsonify({"error": str(e)}), 500
