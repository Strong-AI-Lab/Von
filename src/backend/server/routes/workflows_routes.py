from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Blueprint, jsonify, request
from pymongo.errors import (
    AutoReconnect,
    ConnectionFailure,
    NetworkTimeout,
    PyMongoError,
    ServerSelectionTimeoutError,
)

from ...db.repositories.concepts_repository import ConceptsRepository
from ...services.text_value_service import get_texts_for_concept
from ...services.workflow_episode_service import (
    count_workflow_use_episodes,
    get_workflow_episode_counts_for_workflows,
    get_workflow_usage_aggregates_for_workflows,
    list_workflow_use_episodes,
)
from ...services.namespace_service import (
    coerce_namespace,
    resolve_canonical_namespace,
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
    resolve_workflow_description,
    resolve_workflow_narrative_text,
)
from ...workflows.workflow_definition_identity_service import (
    build_workflow_definition_identity,
    build_workflow_definition_identity_from_graph,
)

logger = logging.getLogger(__name__)

workflows_bp = Blueprint("workflows", __name__)

_WORKFLOW_DEFINITIONS_CACHE_LOCK = threading.Lock()
_WORKFLOW_DEFINITIONS_CACHE: Dict[
    Tuple[int, Optional[str], Optional[str], Optional[str]], Dict[str, Any]
] = {}
_WORKFLOW_DEFINITIONS_REFRESH_LOCKS_LOCK = threading.Lock()
_WORKFLOW_DEFINITIONS_REFRESH_LOCKS: Dict[
    Tuple[int, Optional[str], Optional[str], Optional[str]], threading.Lock
] = {}
_WORKFLOW_DEFINITIONS_CACHE_MAX_ENTRIES = 32
_WORKFLOW_DEFINITIONS_CACHE_TTL_SECONDS_DEFAULT = 8.0
_WORKFLOW_DEFINITIONS_REFRESH_RETRY_AFTER_SECONDS_DEFAULT = 1.0


def _is_transient_workflow_instances_error(exc: Exception) -> bool:
    if isinstance(
        exc,
        (
            NetworkTimeout,
            ServerSelectionTimeoutError,
            AutoReconnect,
            ConnectionFailure,
            PyMongoError,
        ),
    ):
        return True
    message = str(exc).lower()
    transient_markers = (
        "timed out",
        "no primary",
        "replicasetnoprimary",
        "connection pool paused",
        "server selection timeout",
        "networktimeout",
        "temporarily unavailable",
    )
    return any(marker in message for marker in transient_markers)


def _read_workflow_definitions_cache_ttl_seconds() -> float:
    raw = os.getenv(
        "VON_WORKFLOW_DEFINITIONS_CACHE_TTL_SECONDS",
        str(_WORKFLOW_DEFINITIONS_CACHE_TTL_SECONDS_DEFAULT),
    )
    try:
        ttl = float(raw)
    except (TypeError, ValueError):
        return _WORKFLOW_DEFINITIONS_CACHE_TTL_SECONDS_DEFAULT
    return max(0.0, ttl)


def _read_workflow_definitions_refresh_retry_after_seconds() -> float:
    raw = os.getenv(
        "VON_WORKFLOW_DEFINITIONS_REFRESH_RETRY_AFTER_SECONDS",
        str(_WORKFLOW_DEFINITIONS_REFRESH_RETRY_AFTER_SECONDS_DEFAULT),
    )
    try:
        delay_seconds = float(raw)
    except (TypeError, ValueError):
        return _WORKFLOW_DEFINITIONS_REFRESH_RETRY_AFTER_SECONDS_DEFAULT
    return max(0.1, delay_seconds)


def _read_cached_workflow_definitions_entry(
    *,
    cache_key: Tuple[int, Optional[str], Optional[str], Optional[str]],
    bypass_cache: bool,
) -> Tuple[Optional[Dict[str, Any]], bool, Optional[float]]:
    if bypass_cache:
        return None, False, None

    ttl_seconds = _read_workflow_definitions_cache_ttl_seconds()
    if ttl_seconds <= 0.0:
        return None, False, None

    now_monotonic = time.monotonic()
    with _WORKFLOW_DEFINITIONS_CACHE_LOCK:
        entry = _WORKFLOW_DEFINITIONS_CACHE.get(cache_key)
        if not isinstance(entry, dict):
            return None, False, None
        expires_at = entry.get("expires_at_monotonic")
        stored_at = entry.get("stored_at_monotonic")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            _WORKFLOW_DEFINITIONS_CACHE.pop(cache_key, None)
            return None, False, None
        if not isinstance(expires_at, (int, float)):
            _WORKFLOW_DEFINITIONS_CACHE.pop(cache_key, None)
            return None, False, None
        if not isinstance(stored_at, (int, float)):
            _WORKFLOW_DEFINITIONS_CACHE.pop(cache_key, None)
            return None, False, None
        is_fresh = now_monotonic < float(expires_at)
        age_seconds = max(0.0, now_monotonic - float(stored_at))
        return payload, is_fresh, age_seconds


def _read_cached_workflow_definitions(
    *,
    cache_key: Tuple[int, Optional[str], Optional[str], Optional[str]],
    bypass_cache: bool,
) -> Optional[Dict[str, Any]]:
    payload, is_fresh, _age_seconds = _read_cached_workflow_definitions_entry(
        cache_key=cache_key,
        bypass_cache=bypass_cache,
    )
    if is_fresh and isinstance(payload, dict):
        return payload
    return None


def _build_workflow_definitions_cache_metadata(
    *,
    state: str,
    age_seconds: float | None,
    refresh_in_progress: bool,
    retry_after_seconds: float | None = None,
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "state": state,
        "refresh_in_progress": bool(refresh_in_progress),
        "age_seconds": (
            round(float(age_seconds), 3)
            if isinstance(age_seconds, (int, float))
            else None
        ),
        "ttl_seconds": round(_read_workflow_definitions_cache_ttl_seconds(), 3),
    }
    if isinstance(retry_after_seconds, (int, float)) and retry_after_seconds > 0:
        metadata["retry_after_seconds"] = round(float(retry_after_seconds), 3)
    return metadata


def _attach_workflow_definitions_cache_metadata(
    payload: Dict[str, Any],
    *,
    state: str,
    age_seconds: float | None,
    refresh_in_progress: bool,
    retry_after_seconds: float | None = None,
) -> Dict[str, Any]:
    response_payload = dict(payload)
    response_payload["cache"] = _build_workflow_definitions_cache_metadata(
        state=state,
        age_seconds=age_seconds,
        refresh_in_progress=refresh_in_progress,
        retry_after_seconds=retry_after_seconds,
    )
    return response_payload


def _get_workflow_definitions_refresh_lock(
    cache_key: Tuple[int, Optional[str], Optional[str], Optional[str]],
) -> threading.Lock:
    with _WORKFLOW_DEFINITIONS_REFRESH_LOCKS_LOCK:
        lock = _WORKFLOW_DEFINITIONS_REFRESH_LOCKS.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _WORKFLOW_DEFINITIONS_REFRESH_LOCKS[cache_key] = lock
        return lock


def _write_cached_workflow_definitions(
    *,
    cache_key: Tuple[int, Optional[str], Optional[str], Optional[str]],
    payload: Dict[str, Any],
) -> None:
    ttl_seconds = _read_workflow_definitions_cache_ttl_seconds()
    if ttl_seconds <= 0.0:
        return

    now_monotonic = time.monotonic()
    expires_at = now_monotonic + ttl_seconds
    with _WORKFLOW_DEFINITIONS_CACHE_LOCK:
        _WORKFLOW_DEFINITIONS_CACHE[cache_key] = {
            "expires_at_monotonic": expires_at,
            "stored_at_monotonic": now_monotonic,
            "payload": payload,
        }
        if len(_WORKFLOW_DEFINITIONS_CACHE) <= _WORKFLOW_DEFINITIONS_CACHE_MAX_ENTRIES:
            return
        oldest_key = None
        oldest_stamp = float("inf")
        for key, value in _WORKFLOW_DEFINITIONS_CACHE.items():
            stored_at = value.get("stored_at_monotonic")
            if isinstance(stored_at, (int, float)) and float(stored_at) < oldest_stamp:
                oldest_key = key
                oldest_stamp = float(stored_at)
        if oldest_key is not None:
            _WORKFLOW_DEFINITIONS_CACHE.pop(oldest_key, None)


def _build_workflow_definitions_payload(
    *,
    limit: int,
    namespace: str | None,
    session_id: str | None,
    turn_id: str | None,
) -> Dict[str, Any]:
    from ...workflows.durable.registry_factory import (
        build_durable_workflow_registry_read_only,
        get_or_build_workflow_registry_inventory_snapshot,
    )
    from ...services.workflow_discovery_service import (
        classify_workflow_concept_executability,
    )

    # Read-only build avoids concept bootstrap writes on list/introspection paths.
    registry = build_durable_workflow_registry_read_only()
    inventory_snapshot = get_or_build_workflow_registry_inventory_snapshot(
        registry=registry
    )
    if not isinstance(inventory_snapshot, dict):
        inventory_snapshot = {}
    workflow_ids = sorted(list(registry.all_workflow_ids()))
    selected_ids = workflow_ids[:limit]
    usage_aggregate_map = get_workflow_usage_aggregates_for_workflows(selected_ids)
    episode_count_map = get_workflow_episode_counts_for_workflows(
        selected_ids,
        namespace=namespace or None,
        session_id=session_id or None,
        turn_id=turn_id or None,
    )

    items: List[Dict[str, Any]] = []
    for workflow_id in selected_ids:
        registration = registry.get_registration(workflow_id)
        definition = (
            registration.definition if registration is not None else registry.get(workflow_id)
        )

        registration_purpose = registration.purpose if registration is not None else None
        definition_purpose = (
            getattr(definition, "purpose", "") if definition is not None else None
        )
        description, description_source = resolve_workflow_description(
            workflow_id,
            workflow_source=(registration.source if registration is not None else None),
            registration_purpose=registration_purpose,
            definition_purpose=definition_purpose,
        )
        source = "unknown"
        if registration is not None:
            if isinstance(registration.source, str) and registration.source.strip():
                source = registration.source.strip()
        definition_identity = build_workflow_definition_identity(
            workflow_id=workflow_id,
            source=source,
            definition=definition,
            authoritative_definition=(
                definition if source.lower() == "vontology" and definition is not None else None
            ),
        )

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

        try:
            is_executable, executability_reason, executability_detail = (
                classify_workflow_concept_executability(workflow_id)
            )
        except Exception as exc:
            logger.warning(
                "Workflow executability classification failed for %s: %s",
                workflow_id,
                exc,
            )
            is_executable = False
            executability_reason = "classification_error"
            executability_detail = f"classification_error:{type(exc).__name__}"

        items.append(
            {
                "workflow_id": workflow_id,
                "description": description,
                "description_source": description_source,
                "initial_state": initial_state,
                "source": source,
                "definition_identity": definition_identity,
                "attempts": int(attempts) if isinstance(attempts, (int, float)) else 0,
                "completions": (
                    int(completions) if isinstance(completions, (int, float)) else 0
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
                "episodes_count": int(episode_count_map.get(workflow_id, 0)),
                "is_executable": bool(is_executable),
                "executability_reason": executability_reason,
                "executability_detail": executability_detail,
            }
        )

    return {
        "items": items,
        "count": len(items),
        "total": len(workflow_ids),
        "episodes_scope": {
            "namespace": namespace or None,
            "session_id": session_id or None,
            "turn_id": turn_id or None,
        },
        "parity_inventory": inventory_snapshot,
    }


def _refresh_workflow_definitions_cache_entry(
    *,
    cache_key: Tuple[int, Optional[str], Optional[str], Optional[str]],
    limit: int,
    namespace: str | None,
    session_id: str | None,
    turn_id: str | None,
    refresh_lock: threading.Lock,
) -> None:
    try:
        payload = _build_workflow_definitions_payload(
            limit=limit,
            namespace=namespace,
            session_id=session_id,
            turn_id=turn_id,
        )
        _write_cached_workflow_definitions(cache_key=cache_key, payload=payload)
    except Exception:
        logger.exception(
            "Background workflow definitions refresh failed for key=%s",
            cache_key,
        )
    finally:
        refresh_lock.release()


def _start_workflow_definitions_background_refresh(
    *,
    cache_key: Tuple[int, Optional[str], Optional[str], Optional[str]],
    limit: int,
    namespace: str | None,
    session_id: str | None,
    turn_id: str | None,
    refresh_lock: threading.Lock,
) -> bool:
    try:
        thread = threading.Thread(
            target=_refresh_workflow_definitions_cache_entry,
            kwargs={
                "cache_key": cache_key,
                "limit": limit,
                "namespace": namespace,
                "session_id": session_id,
                "turn_id": turn_id,
                "refresh_lock": refresh_lock,
            },
            name="workflow-definitions-refresh",
            daemon=True,
        )
        thread.start()
        return True
    except Exception:
        logger.exception(
            "Could not start background workflow definitions refresh for key=%s",
            cache_key,
        )
        return False


@workflows_bp.get("/api/workflows/definitions/<path:workflow_id>")
def api_get_workflow_definition(workflow_id: str):
    definition, warnings = build_workflow_process_graph(workflow_id)
    raw, raw_source = resolve_workflow_narrative_text(workflow_id)

    if not definition:
        # Backward compatibility: if a workflow only has narrative text, return it.
        # The preferred representation is explicit step/control-flow relationships.
        if raw:
            return jsonify(
                {
                    "workflow_id": workflow_id,
                    "definition": {"representation": "narrative_only"},
                    "raw": raw,
                    "raw_source": raw_source,
                    "definition_identity": None,
                    "warnings": warnings,
                }
            )

        return (
            jsonify(
                {"error": "workflow_definition_not_found", "workflow_id": workflow_id}
            ),
            404,
        )

    definition_identity = build_workflow_definition_identity_from_graph(
        workflow_id=workflow_id,
        graph=definition if isinstance(definition, dict) else None,
    )

    return jsonify(
        {
            "workflow_id": workflow_id,
            "definition": definition,
            "raw": raw,
            "raw_source": raw_source,
            "definition_identity": definition_identity,
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
    namespace = request.args.get("namespace")
    session_id = request.args.get("session_id")
    turn_id = request.args.get("turn_id")
    bypass_cache = request.args.get("nocache", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    cache_key = (limit, namespace or None, session_id or None, turn_id or None)
    refresh_lock: threading.Lock | None = None
    refresh_lock_acquired = False
    try:
        cached_payload, cached_is_fresh, cached_age_seconds = (
            _read_cached_workflow_definitions_entry(
                cache_key=cache_key,
                bypass_cache=bypass_cache,
            )
        )
        if cached_is_fresh and isinstance(cached_payload, dict):
            return jsonify(
                _attach_workflow_definitions_cache_metadata(
                    cached_payload,
                    state="fresh",
                    age_seconds=cached_age_seconds,
                    refresh_in_progress=False,
                )
            )

        retry_after_seconds = _read_workflow_definitions_refresh_retry_after_seconds()

        # Serve expired payloads immediately and refresh in the background so the
        # monitor stays responsive even when registry rebuilds take longer than
        # the UI timeout budget. The cache metadata keeps stale responses explicit.
        if isinstance(cached_payload, dict) and not bypass_cache:
            refresh_lock = _get_workflow_definitions_refresh_lock(cache_key)
            refresh_lock_acquired = refresh_lock.acquire(blocking=False)
            if refresh_lock_acquired:
                refresh_started = _start_workflow_definitions_background_refresh(
                    cache_key=cache_key,
                    limit=limit,
                    namespace=namespace or None,
                    session_id=session_id or None,
                    turn_id=turn_id or None,
                    refresh_lock=refresh_lock,
                )
                if refresh_started:
                    refresh_lock_acquired = False
                    logger.info(
                        "[workflow_definitions] Serving stale cache and refreshing in background for key=%s",
                        cache_key,
                    )
                    return jsonify(
                        _attach_workflow_definitions_cache_metadata(
                            cached_payload,
                            state="stale",
                            age_seconds=cached_age_seconds,
                            refresh_in_progress=True,
                            retry_after_seconds=retry_after_seconds,
                        )
                    )
            else:
                logger.info(
                    "[workflow_definitions] Serving stale cache while refresh in progress for key=%s",
                    cache_key,
                )
                return jsonify(
                    _attach_workflow_definitions_cache_metadata(
                        cached_payload,
                        state="stale",
                        age_seconds=cached_age_seconds,
                        refresh_in_progress=True,
                        retry_after_seconds=retry_after_seconds,
                    )
                )

        if not bypass_cache and not refresh_lock_acquired:
            refresh_lock = _get_workflow_definitions_refresh_lock(cache_key)
            refresh_lock_acquired = refresh_lock.acquire(blocking=False)
            if not refresh_lock_acquired:
                payload = {
                    "error": "workflow_definitions_refresh_in_progress",
                    "detail": "Workflow definitions refresh is already running; retry shortly.",
                    "retryable": True,
                    "retry_after_seconds": retry_after_seconds,
                }
                response = jsonify(payload)
                response.status_code = 503
                response.headers["Retry-After"] = str(
                    max(1, int(math.ceil(retry_after_seconds)))
                )
                return response

        payload = _build_workflow_definitions_payload(
            limit=limit,
            namespace=namespace or None,
            session_id=session_id or None,
            turn_id=turn_id or None,
        )
        _write_cached_workflow_definitions(cache_key=cache_key, payload=payload)
        return jsonify(
            _attach_workflow_definitions_cache_metadata(
                payload,
                state="fresh",
                age_seconds=0.0,
                refresh_in_progress=False,
            )
        )
    except Exception as exc:
        logger.exception("Failed to list workflow definitions via API")
        return jsonify({"error": "workflow_definitions_list_failed", "detail": str(exc)}), 500
    finally:
        if refresh_lock is not None and refresh_lock_acquired:
            refresh_lock.release()


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

    filters = {
        "workflow_id": workflow_id or None,
        "namespace": namespace or None,
        "session_id": session_id or None,
        "turn_id": turn_id or None,
    }

    try:
        items = list_workflow_use_episodes(
            workflow_id=workflow_id or None,
            namespace=namespace or None,
            session_id=session_id or None,
            turn_id=turn_id or None,
            limit=limit,
        )
        total = count_workflow_use_episodes(
            workflow_id=workflow_id or None,
            namespace=namespace or None,
            session_id=session_id or None,
            turn_id=turn_id or None,
        )
        return jsonify(
            {
                "items": items,
                "count": len(items),
                "total": int(total),
                "has_more": bool(total > len(items)),
                "filters": filters,
            }
        )
    except Exception as exc:
        logger.exception(
            "Failed to list workflow episodes",
            extra={"filters": filters},
        )
        detail = str(exc).strip() or type(exc).__name__
        return (
            jsonify(
                {
                    "error": "workflow_episodes_fetch_failed",
                    "detail": detail,
                    "error_type": type(exc).__name__,
                    "filters": filters,
                    "diagnostics": {
                        "backend_reason": detail,
                        "namespace": filters.get("namespace"),
                    },
                }
            ),
            500,
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
        "user_id": "#V#user_123",
        "org_id": "#V#org_456",
        "namespace": "#V#user_123@org_456",
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
    namespace = resolve_canonical_namespace(
        data.get("namespace"),
        user_id,
        org_id,
    )
    if not namespace:
        return (
            jsonify(
                {
                    "error": "namespace must be canonical or derivable from user_id/org_id",
                }
            ),
            400,
        )
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
    namespace = coerce_namespace(request.args.get("namespace")) or request.args.get(
        "namespace"
    )
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
    try:
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
    except Exception as exc:
        if _is_transient_workflow_instances_error(exc):
            logger.warning(
                "Workflow instances snapshot degraded due to transient store error: %s",
                exc,
                exc_info=True,
            )
            response = jsonify(
                {
                    "items": [],
                    "count": 0,
                    "degraded": True,
                    "retryable": True,
                    "retry_after_seconds": 2,
                    "error": "Workflow monitor temporarily unavailable; please retry.",
                    "detail": str(exc)[:300],
                }
            )
            response.status_code = 503
            response.headers["Retry-After"] = "2"
            return response
        logger.exception("Failed to list workflow instances")
        return jsonify({"error": str(exc)}), 500

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
        "user_id": "#V#user_123",
        "org_id": "#V#org_456",
        "namespace": "#V#user_123@org_456",
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
    namespace = resolve_canonical_namespace(
        data.get("namespace"),
        user_id,
        org_id,
    )
    if not namespace:
        return (
            jsonify(
                {
                    "error": "namespace must be canonical or derivable from user_id/org_id",
                }
            ),
            400,
        )
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
