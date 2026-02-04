from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Blueprint, jsonify, request

from ...db.repositories.concepts_repository import ConceptsRepository
from ...services.text_value_service import get_texts_for_concept
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

logger = logging.getLogger(__name__)

workflows_bp = Blueprint("workflows", __name__)


def _normalise_relationship_targets(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def _first_relationship_target(
    relationships: Dict[str, Any], candidate_predicates: Iterable[str]
) -> Optional[str]:
    for predicate in candidate_predicates:
        targets = _normalise_relationship_targets(relationships.get(predicate))
        if targets:
            return targets[0]
    return None


def _all_relationship_targets(
    relationships: Dict[str, Any], candidate_predicates: Iterable[str]
) -> List[str]:
    results: List[str] = []
    for predicate in candidate_predicates:
        results.extend(_normalise_relationship_targets(relationships.get(predicate)))
    # Preserve order but remove duplicates.
    seen: set[str] = set()
    unique: List[str] = []
    for item in results:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _best_effort_workflow_narrative_text(workflow_id: str) -> Optional[str]:
    """Fetch a stored workflow definition *narrative* text from Vontology.

    Workflows should be represented structurally via explicit relationships
    (steps + control flow). This text is optional and is not machine-parsed.
    """

    if not isinstance(workflow_id, str) or not workflow_id.strip():
        return None

    try:
        texts = get_texts_for_concept(workflow_id)
    except Exception:
        return None

    if not isinstance(texts, list):
        return None

    preferred_predicates = ("hasDefinition", "hasContent", "hasDescription")

    for predicate in preferred_predicates:
        for item in texts:
            if not isinstance(item, dict):
                continue
            if item.get("predicate") != predicate:
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()

    return None


def _fetch_concepts_by_id(concept_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not concept_ids:
        return {}

    cursor = ConceptsRepository.find(
        {"concept_id": {"$in": concept_ids}},
        {"concept_id": 1, "name": 1, "relationships": 1},
        limit=len(concept_ids),
    )
    docs = list(cursor)
    mapping: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        cid = doc.get("concept_id")
        if isinstance(cid, str) and cid.strip():
            mapping[cid] = doc
    return mapping


def _build_workflow_process_graph(
    workflow_id: str,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Build a workflow definition from explicit Vontology relationships.

    Expected (flexible) predicate names:
    - workflow -> hasInitialStep / has_initial_step
    - workflow -> hasStep / has_step
    - step -> invokesAction / invokes_action
    - step -> nextStep / next_step
    - step -> onTrueNextStep / on_true_next_step
    - step -> onFalseNextStep / on_false_next_step
    - step -> onFailureNextStep / on_failure_next_step
    """

    warnings: List[str] = []

    workflow_doc = ConceptsRepository.find_one(
        {"concept_id": workflow_id},
        {"concept_id": 1, "name": 1, "relationships": 1},
    )
    if not workflow_doc:
        return None, ["workflow_concept_not_found"]

    relationships = workflow_doc.get("relationships") or {}
    if not isinstance(relationships, dict):
        relationships = {}

    initial_step = _first_relationship_target(
        relationships,
        (
            "hasInitialStep",
            "has_initial_step",
            "#V#hasInitialStep",
            "#V#has_initial_step",
        ),
    )
    step_ids = _all_relationship_targets(
        relationships, ("hasStep", "has_step", "#V#hasStep", "#V#has_step")
    )

    if initial_step and initial_step not in step_ids:
        step_ids.insert(0, initial_step)
    if not initial_step and step_ids:
        initial_step = step_ids[0]
        warnings.append("missing_hasInitialStep_used_first_hasStep")

    if not step_ids:
        return None, ["workflow_has_no_steps"]

    step_docs = _fetch_concepts_by_id(step_ids)
    missing_steps = [sid for sid in step_ids if sid not in step_docs]
    if missing_steps:
        warnings.append(f"missing_step_concepts:{','.join(missing_steps[:25])}")

    step_items: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    def _edge(source_id: str, predicate: str, target_id: Optional[str]) -> None:
        if not target_id:
            return
        edges.append({"from": source_id, "predicate": predicate, "to": target_id})

    for step_id in step_ids:
        doc = step_docs.get(step_id, {})
        step_rels = doc.get("relationships") or {}
        if not isinstance(step_rels, dict):
            step_rels = {}

        invokes_action = _first_relationship_target(
            step_rels,
            (
                "invokesAction",
                "invokes_action",
                "#V#invokesAction",
                "#V#invokes_action",
            ),
        )
        next_step = _first_relationship_target(
            step_rels, ("nextStep", "next_step", "#V#nextStep", "#V#next_step")
        )
        on_true = _first_relationship_target(
            step_rels,
            (
                "onTrueNextStep",
                "on_true_next_step",
                "#V#onTrueNextStep",
                "#V#on_true_next_step",
            ),
        )
        on_false = _first_relationship_target(
            step_rels,
            (
                "onFalseNextStep",
                "on_false_next_step",
                "#V#onFalseNextStep",
                "#V#on_false_next_step",
            ),
        )
        on_failure = _first_relationship_target(
            step_rels,
            (
                "onFailureNextStep",
                "on_failure_next_step",
                "#V#onFailureNextStep",
                "#V#on_failure_next_step",
            ),
        )

        preconditions = _all_relationship_targets(
            step_rels,
            (
                "hasPrecondition",
                "has_precondition",
                "#V#hasPrecondition",
                "#V#has_precondition",
            ),
        )
        effects = _all_relationship_targets(
            step_rels, ("hasEffect", "has_effect", "#V#hasEffect", "#V#has_effect")
        )
        reads_vars = _all_relationship_targets(
            step_rels,
            (
                "readsVariable",
                "reads_variable",
                "#V#readsVariable",
                "#V#reads_variable",
            ),
        )
        writes_vars = _all_relationship_targets(
            step_rels,
            (
                "writesVariable",
                "writes_variable",
                "#V#writesVariable",
                "#V#writes_variable",
            ),
        )

        step_items.append(
            {
                "step_id": step_id,
                "name": doc.get("name"),
                "invokes_action": invokes_action,
                "preconditions": preconditions,
                "effects": effects,
                "reads_variables": reads_vars,
                "writes_variables": writes_vars,
                "control_flow": {
                    "next": next_step,
                    "on_true": on_true,
                    "on_false": on_false,
                    "on_failure": on_failure,
                },
            }
        )

        _edge(step_id, "nextStep", next_step)
        _edge(step_id, "onTrueNextStep", on_true)
        _edge(step_id, "onFalseNextStep", on_false)
        _edge(step_id, "onFailureNextStep", on_failure)

    definition = {
        "representation": "vontology_process_graph_v1",
        "workflow_id": workflow_id,
        "initial_step": initial_step,
        "steps": step_items,
        "edges": edges,
        "warnings": warnings,
    }
    return definition, warnings


@workflows_bp.get("/api/workflows/definitions/<path:workflow_id>")
def api_get_workflow_definition(workflow_id: str):
    definition, warnings = _build_workflow_process_graph(workflow_id)
    raw = _best_effort_workflow_narrative_text(workflow_id)

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
        instance_id = manager.create_instance(
            workflow_id,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            inputs=inputs,
            max_retries=max_retries,
        )
        return jsonify({"instance_id": instance_id, "status": "pending"}), 201
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
    - limit: Maximum results (default 50)
    """
    user_id = request.args.get("user_id")
    org_id = request.args.get("org_id")
    namespace = request.args.get("namespace")
    status_str = request.args.get("status")
    workflow_id = request.args.get("workflow_id")
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
        instance_id = manager.create_instance(
            schedule.workflow_id,
            user_id=schedule.user_id,
            org_id=schedule.org_id,
            namespace=schedule.namespace,
            inputs=schedule.default_inputs,
            schedule_id=schedule.schedule_id,
        )
        return (
            jsonify(
                {
                    "instance_id": instance_id,
                    "schedule_id": schedule_id,
                    "status": "triggered",
                }
            ),
            201,
        )
    except Exception as e:
        logger.exception("Failed to trigger workflow schedule")
        return jsonify({"error": str(e)}), 500
