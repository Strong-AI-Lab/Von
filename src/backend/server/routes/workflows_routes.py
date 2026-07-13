from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request, session

from ...db.transient_errors import is_transient_mongo_error
from ...security.access_control import (
    cache_scope_key,
    can_access_concept,
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
)
from ...services.workflow_episode_service import (
    count_workflow_use_episodes,
    get_workflow_episode_counts_for_workflows,
    get_workflow_usage_aggregates_for_workflows,
    list_workflow_use_episodes,
)
from ...services.workflow_capability_service import (
    get_workflow_capability_index_readiness_report,
)
from ...services.workflow_prediction_service import (
    build_workflow_prediction_envelope,
)
from ...workflows.trace_store import (
    get_workflow_execution_trace,
    list_recent_workflow_execution_traces,
)
from ...workflows.durable import (
    WorkflowInstanceStatus,
    WorkflowSchedule,
    ScheduleType,
    WorkflowInstanceManager,
)
from ...workflows.durable.workflow_instance_submission_service import (
    submit_verified_workflow_instance,
)
from ...workflows.durable.registry_factory import (
    WorkflowDefinitionAuthorityTransientError,
    resolve_workflow_definition_from_authority,
)
from ...workflows.vontology_loader import (
    build_workflow_process_graph,
    resolve_workflow_narrative_text,
)
from ...workflows.workflow_definition_identity_service import (
    build_workflow_definition_identity_from_graph,
)
from ...workflows.workflow_listing_service import (
    build_workflow_listing_entry,
    collect_workflow_introspection_projection_ids,
    filter_workflow_ids_for_current_actor,
    project_workflow_introspection_payload_for_current_actor,
)
from ...workflows.workflow_studio_service import (
    WorkflowStudioAuthorityError,
    WorkflowStudioConflictError,
    apply_workflow_authoring_spec,
    build_workflow_catalogue_payload,
    build_workflow_description_proposal,
    build_workflow_studio_detail_payload,
    demote_workflow_routing,
    preview_workflow_authoring_spec,
    require_workflow_studio_mutation_actor,
    review_workflow_authoring_proposal,
    rollback_workflow_authoring_promotion,
    submit_workflow_authoring_proposal,
    supersede_workflow_publication,
)

logger = logging.getLogger(__name__)

workflows_bp = Blueprint("workflows", __name__)

_WORKFLOW_DEFINITIONS_CACHE_LOCK = threading.Lock()
_WORKFLOW_DEFINITIONS_CACHE: Dict[
    Tuple[int, Optional[str], Optional[str], Optional[str], str], Dict[str, Any]
] = {}
_WORKFLOW_DEFINITIONS_REFRESH_LOCKS_LOCK = threading.Lock()
_WORKFLOW_DEFINITIONS_REFRESH_LOCKS: Dict[
    Tuple[int, Optional[str], Optional[str], Optional[str], str], threading.Lock
] = {}
_WORKFLOW_DEFINITIONS_CACHE_MAX_ENTRIES = 32
_WORKFLOW_DEFINITIONS_CACHE_TTL_SECONDS_DEFAULT = 8.0
_WORKFLOW_DEFINITIONS_REFRESH_RETRY_AFTER_SECONDS_DEFAULT = 1.0
_WORKFLOW_DEFINITIONS_EXECUTABILITY_PENDING_REASON = "inspection_summary_pending"
_WORKFLOW_DEFINITIONS_EXECUTABILITY_PENDING_DETAIL = "lazy_definition_not_loaded"
_WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE_LOCK = threading.Lock()
_WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE: Dict[str, Any] = {}
_WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE_TTL_SECONDS_DEFAULT = 15.0


def _safe_request_arg(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _resolve_request_llm_scope_ids() -> Tuple[Optional[str], Optional[str]]:
    user_concept_id: Optional[str] = None
    org_concept_id: Optional[str] = None
    try:
        resolved_user_concept_id = get_effective_user_concept_id()
        if isinstance(resolved_user_concept_id, str):
            user_concept_id = resolved_user_concept_id.strip() or None
    except Exception:
        user_concept_id = None
    try:
        resolved_org_concept_id = session.get("organisation_concept_id")
        if isinstance(resolved_org_concept_id, str):
            org_concept_id = resolved_org_concept_id.strip() or None
    except Exception:
        org_concept_id = None
    return user_concept_id, org_concept_id


def _is_transient_workflow_instances_error(exc: Exception) -> bool:
    if is_transient_mongo_error(exc):
        return True
    message = str(exc).lower()
    transient_markers = (
        "temporarily unavailable",
    )
    return any(marker in message for marker in transient_markers)


def _build_retryable_workflow_instances_response(
    *,
    error: str,
    detail: str,
    retry_after_seconds: int = 2,
    payload: dict[str, Any] | None = None,
):
    body = dict(payload or {})
    body.update(
        {
            "degraded": True,
            "retryable": True,
            "retry_after_seconds": retry_after_seconds,
            "error": error,
            "detail": detail[:300],
        }
    )
    response = jsonify(body)
    response.status_code = 503
    response.headers["Retry-After"] = str(max(1, int(retry_after_seconds)))
    return response


def _parse_workflow_instance_status_filters(
    status_raw: str | None,
) -> list[WorkflowInstanceStatus]:
    """Parse and validate workflow instance status filters from query params."""
    if not isinstance(status_raw, str) or not status_raw.strip():
        return []

    statuses: list[WorkflowInstanceStatus] = []
    seen: set[str] = set()
    for raw_status in status_raw.split(","):
        value = raw_status.strip().lower()
        if not value or value in seen:
            continue
        statuses.append(WorkflowInstanceStatus(value))
        seen.add(value)
    return statuses


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


def _read_workflow_capability_index_status_cache_ttl_seconds() -> float:
    raw = os.getenv(
        "VON_WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE_TTL_SECONDS",
        str(_WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE_TTL_SECONDS_DEFAULT),
    )
    try:
        ttl = float(raw)
    except (TypeError, ValueError):
        return _WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE_TTL_SECONDS_DEFAULT
    return max(0.0, ttl)


def _read_cached_workflow_capability_index_status(
    *, bypass_cache: bool
) -> Tuple[Optional[Dict[str, Any]], Optional[float]]:
    if bypass_cache:
        return None, None

    ttl_seconds = _read_workflow_capability_index_status_cache_ttl_seconds()
    if ttl_seconds <= 0.0:
        return None, None

    now_monotonic = time.monotonic()
    with _WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE_LOCK:
        stored_at = _WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE.get("stored_at_monotonic")
        payload = _WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE.get("payload")
        if not isinstance(stored_at, (int, float)) or not isinstance(payload, dict):
            return None, None
        age_seconds = max(0.0, now_monotonic - float(stored_at))
        if age_seconds > ttl_seconds:
            _WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE.clear()
            return None, None
        return dict(payload), age_seconds


def _write_cached_workflow_capability_index_status(payload: Dict[str, Any]) -> None:
    ttl_seconds = _read_workflow_capability_index_status_cache_ttl_seconds()
    if ttl_seconds <= 0.0:
        return
    with _WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE_LOCK:
        _WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE.clear()
        _WORKFLOW_CAPABILITY_INDEX_STATUS_CACHE.update(
            {
                "stored_at_monotonic": time.monotonic(),
                "payload": dict(payload),
            }
        )


_PUBLIC_WORKFLOW_CAPABILITY_INDEX_STATUSES = {
    "ready",
    "building",
    "rebuilding",
    "warming",
    "rebuild_required",
    "not_ready",
    "error",
    "timeout",
}


def _actor_visible_workflow_count() -> int | None:
    """Return a fail-closed actor-scoped workflow count for public status UI."""

    try:
        from ...workflows.durable.registry_factory import (
            build_durable_workflow_registry_read_only,
        )

        registry = build_durable_workflow_registry_read_only(
            defer_parity_work=True
        )
        return len(
            filter_workflow_ids_for_current_actor(registry.all_workflow_ids())
        )
    except Exception:
        logger.warning(
            "Could not derive actor-visible workflow count for capability status",
            exc_info=True,
        )
        return None


def _build_actor_safe_workflow_capability_index_status(
    payload: Dict[str, Any],
    *,
    visible_workflow_count: int | None = None,
) -> Dict[str, Any]:
    """Project global capability diagnostics into a bounded public readiness view.

    The capability-index runtime state is process-global and contains raw build
    errors, filesystem paths, invalidation reasons, and hidden-inclusive counts.
    Those fields are useful to operator diagnostics but are not safe on an
    actor-facing workflow route.  Derive all prose from a closed status set and
    recompute the only workflow count under ambient actor authority.
    """

    ready = bool(payload.get("ready", False))
    raw_status = str(payload.get("status") or "").strip().lower()
    if ready:
        status = "ready"
    elif raw_status in _PUBLIC_WORKFLOW_CAPABILITY_INDEX_STATUSES:
        status = raw_status
    else:
        status = "not_ready"

    if status == "ready":
        summary = "Workflow capability index ready."
        detail = (
            "Workflow discovery is available for workflows visible to the "
            "current actor."
        )
    elif status in {"building", "rebuilding", "warming", "timeout"}:
        summary = "Workflow capability index is still initialising."
        detail = (
            "Workflow discovery is waiting for the shared capability index to "
            "become ready."
        )
    elif status == "rebuild_required":
        summary = "Workflow capability index rebuild required."
        detail = (
            "Workflow discovery is unavailable until an operator rebuilds the "
            "shared capability index."
        )
    elif status == "error":
        summary = "Workflow capability index not ready."
        detail = (
            "Workflow discovery is unavailable. Operator diagnostics contain "
            "the underlying failure details."
        )
    else:
        summary = "Workflow capability index not ready."
        detail = "No ready workflow capability snapshot is currently available."

    if not isinstance(visible_workflow_count, int) or visible_workflow_count < 0:
        visible_workflow_count = _actor_visible_workflow_count()
    checked_at_utc = None
    raw_checked_at_utc = payload.get("checked_at_utc")
    if isinstance(raw_checked_at_utc, str) and raw_checked_at_utc.strip():
        candidate_checked_at_utc = raw_checked_at_utc.strip()
        try:
            datetime.fromisoformat(candidate_checked_at_utc.replace("Z", "+00:00"))
            checked_at_utc = candidate_checked_at_utc
        except ValueError:
            checked_at_utc = None
    warning_level = (
        "ok"
        if ready
        else ("error" if status in {"error", "rebuild_required"} else "warning")
    )
    return {
        "schema_version": "workflow_capability_index_public_status.v1",
        "ready": ready,
        "status": status,
        "warning_level": warning_level,
        "workflow_discovery_available": ready,
        "user_visible_blocker": not ready,
        "user_visible_severity": "ok" if ready else "error",
        "footer_red_flag": not ready,
        "build_in_progress": bool(payload.get("build_in_progress", False)),
        "summary": summary,
        "detail": detail,
        "visible_workflow_count": visible_workflow_count,
        "visible_workflow_count_available": visible_workflow_count is not None,
        "workflow_count_scope": "actor_visible",
        "checked_at_utc": checked_at_utc,
    }


def _attach_workflow_capability_index_status_cache_metadata(
    payload: Dict[str, Any],
    *,
    state: str,
    age_seconds: float | None,
) -> Dict[str, Any]:
    response_payload = _build_actor_safe_workflow_capability_index_status(payload)
    response_payload["cache"] = {
        "state": state,
        "age_seconds": (
            round(float(age_seconds), 3)
            if isinstance(age_seconds, (int, float))
            else None
        ),
        "ttl_seconds": round(
            _read_workflow_capability_index_status_cache_ttl_seconds(), 3
        ),
    }
    return response_payload


def _read_cached_workflow_definitions_entry(
    *,
    cache_key: Tuple[int, Optional[str], Optional[str], Optional[str], str],
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
    cache_key: Tuple[int, Optional[str], Optional[str], Optional[str], str],
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
    cached_workflow_ids = payload.get("_introspection_workflow_ids")
    cached_total_workflow_ids = payload.get("_introspection_total_workflow_ids")
    response_payload = project_workflow_introspection_payload_for_current_actor(
        payload,
        workflow_ids=(
            cached_workflow_ids if isinstance(cached_workflow_ids, list) else ()
        ),
        total_workflow_ids=(
            cached_total_workflow_ids
            if isinstance(cached_total_workflow_ids, list)
            else None
        ),
    )
    raw_capability_index = response_payload.get("capability_index")
    projected_total = response_payload.get("total")
    response_payload["capability_index"] = (
        _build_actor_safe_workflow_capability_index_status(
            raw_capability_index if isinstance(raw_capability_index, dict) else {},
            visible_workflow_count=(
                projected_total if isinstance(projected_total, int) else None
            ),
        )
    )
    response_payload.pop("_introspection_workflow_ids", None)
    response_payload.pop("_introspection_total_workflow_ids", None)
    response_payload["cache"] = _build_workflow_definitions_cache_metadata(
        state=state,
        age_seconds=age_seconds,
        refresh_in_progress=refresh_in_progress,
        retry_after_seconds=retry_after_seconds,
    )
    return response_payload


def _get_workflow_definitions_refresh_lock(
    cache_key: Tuple[int, Optional[str], Optional[str], Optional[str], str],
) -> threading.Lock:
    with _WORKFLOW_DEFINITIONS_REFRESH_LOCKS_LOCK:
        lock = _WORKFLOW_DEFINITIONS_REFRESH_LOCKS.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _WORKFLOW_DEFINITIONS_REFRESH_LOCKS[cache_key] = lock
        return lock


def _write_cached_workflow_definitions(
    *,
    cache_key: Tuple[int, Optional[str], Optional[str], Optional[str], str],
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


def _clear_workflow_definitions_cache() -> None:
    with _WORKFLOW_DEFINITIONS_CACHE_LOCK:
        _WORKFLOW_DEFINITIONS_CACHE.clear()


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
    registry = build_durable_workflow_registry_read_only(defer_parity_work=True)
    inventory_snapshot = get_or_build_workflow_registry_inventory_snapshot(
        registry=registry,
        allow_sync_build=False,
    )
    if not isinstance(inventory_snapshot, dict):
        inventory_snapshot = {}
    all_workflow_ids = sorted(list(registry.all_workflow_ids()))
    introspection_workflow_ids = collect_workflow_introspection_projection_ids(
        all_workflow_ids,
        parity_inventory=inventory_snapshot,
    )
    workflow_ids = filter_workflow_ids_for_current_actor(all_workflow_ids)
    selected_ids = workflow_ids[:limit]
    usage_aggregate_map = get_workflow_usage_aggregates_for_workflows(
        selected_ids,
        namespace=namespace,
        session_id=session_id,
        turn_id=turn_id,
        strict_namespace_scope=True,
    )
    episode_count_map = get_workflow_episode_counts_for_workflows(
        selected_ids,
        namespace=namespace,
        session_id=session_id or None,
        turn_id=turn_id or None,
        strict_namespace_scope=True,
    )

    items: List[Dict[str, Any]] = []
    for workflow_id in selected_ids:
        listing_entry = build_workflow_listing_entry(
            registry=registry,
            workflow_id=workflow_id,
        )
        usage = (
            usage_aggregate_map.get(workflow_id, {})
            if isinstance(usage_aggregate_map, dict)
            else {}
        )
        attempts = usage.get("attempts")
        completions = usage.get("completions")
        completion_rate = usage.get("completion_rate")

        if not bool(listing_entry.get("definition_loaded")):
            is_executable = False
            executability_reason = _WORKFLOW_DEFINITIONS_EXECUTABILITY_PENDING_REASON
            executability_detail = _WORKFLOW_DEFINITIONS_EXECUTABILITY_PENDING_DETAIL
        else:
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
                **listing_entry,
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

    payload = {
        # Retained only inside the cache so a response can be re-projected
        # against ambient actor authority after an unscoped background refresh.
        "_introspection_workflow_ids": introspection_workflow_ids,
        "_introspection_total_workflow_ids": all_workflow_ids,
        "items": items,
        "count": len(items),
        "total": len(workflow_ids),
        "episodes_scope": {
            "namespace": namespace or None,
            "session_id": session_id or None,
            "turn_id": turn_id or None,
        },
        "parity_inventory": inventory_snapshot,
        "capability_index": get_workflow_capability_index_readiness_report(),
    }
    projected_payload = project_workflow_introspection_payload_for_current_actor(
        payload,
        workflow_ids=introspection_workflow_ids,
        total_workflow_ids=all_workflow_ids,
    )
    projected_payload["_introspection_workflow_ids"] = introspection_workflow_ids
    projected_payload["_introspection_total_workflow_ids"] = all_workflow_ids
    return projected_payload


def _refresh_workflow_definitions_cache_entry(
    *,
    cache_key: Tuple[int, Optional[str], Optional[str], Optional[str], str],
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
    cache_key: Tuple[int, Optional[str], Optional[str], Optional[str], str],
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
    if workflow_id not in filter_workflow_ids_for_current_actor([workflow_id]):
        return (
            jsonify(
                {
                    "error": "workflow_definition_not_found",
                    "workflow_id": workflow_id,
                }
            ),
            404,
        )

    # Root visibility alone is insufficient: a globally warmed process graph
    # can reference restricted steps, prompts, actions, or mappings. Require the
    # actor-scoped executable loader to prove the complete graph before any raw
    # graph/narrative projection is built.
    try:
        authority = resolve_workflow_definition_from_authority(
            workflow_id,
            actor_user_id=get_effective_user_concept_id(),
            actor_org_id=get_effective_organisation_concept_id(),
        )
    except WorkflowDefinitionAuthorityTransientError:
        return (
            jsonify(
                {
                    "error": "workflow_definition_temporarily_unavailable",
                    "workflow_id": workflow_id,
                }
            ),
            503,
        )
    if authority.definition is None:
        return (
            jsonify(
                {
                    "error": "workflow_definition_not_found",
                    "workflow_id": workflow_id,
                }
            ),
            404,
        )

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
    claimed_namespace = request.args.get("namespace")
    ambient_user_id = get_effective_user_concept_id()
    ambient_org_id = get_effective_organisation_concept_id()
    if ambient_user_id or ambient_org_id:
        actor_scope, actor_error_response = _resolve_http_workflow_actor_scope(
            claimed_namespace=claimed_namespace,
        )
        if actor_error_response is not None:
            return actor_error_response
        namespace = getattr(actor_scope, "namespace", None)
    elif claimed_namespace:
        return (
            jsonify(
                {
                    "error": "workflow_actor_authority_required",
                    "error_code": "workflow_actor_authority_required",
                }
            ),
            403,
        )
    else:
        # Public workflow metadata may remain visible, but metrics have no
        # provable tenant scope and therefore resolve to zero under the strict
        # episode query used below.
        namespace = None
    session_id = request.args.get("session_id")
    turn_id = request.args.get("turn_id")
    bypass_cache = request.args.get("nocache", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    cache_key = (
        limit,
        namespace or None,
        session_id or None,
        turn_id or None,
        cache_scope_key(),
    )
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


@workflows_bp.get("/api/workflows/capability-index/status")
def api_get_workflow_capability_index_status():
    """Return the authoritative workflow capability-index readiness state."""

    try:
        bypass_cache = request.args.get("nocache", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        cached_payload, cached_age_seconds = (
            _read_cached_workflow_capability_index_status(
                bypass_cache=bypass_cache,
            )
        )
        if isinstance(cached_payload, dict):
            return jsonify(
                _attach_workflow_capability_index_status_cache_metadata(
                    cached_payload,
                    state="fresh",
                    age_seconds=cached_age_seconds,
                )
            )

        payload = get_workflow_capability_index_readiness_report()
        if isinstance(payload, dict):
            _write_cached_workflow_capability_index_status(payload)
            return jsonify(
                _attach_workflow_capability_index_status_cache_metadata(
                    payload,
                    state="computed",
                    age_seconds=0.0,
                )
            )
        return jsonify(payload)
    except Exception as exc:
        logger.exception("Failed to read workflow capability index status")
        return (
            jsonify(
                {
                    "error": "workflow_capability_index_status_failed",
                    "detail": str(exc),
                }
            ),
            500,
        )


@workflows_bp.get("/api/workflow-studio/catalogue")
def api_workflow_studio_catalogue():
    limit_raw = request.args.get("limit", "250")
    try:
        limit = int(limit_raw)
    except Exception:
        limit = 250
    limit = max(1, min(limit, 500))
    namespace = request.args.get("namespace")
    session_id = request.args.get("session_id")
    turn_id = request.args.get("turn_id")
    include_designs = request.args.get("include_designs", "true").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    try:
        payload = build_workflow_catalogue_payload(
            limit=limit,
            namespace=namespace or None,
            session_id=session_id or None,
            turn_id=turn_id or None,
            include_designs=include_designs,
        )
        payload["studio"] = {
            "independent_surface": True,
            "read_model": "workflow_studio.read_model.v1",
            "include_designs": include_designs,
        }
        return jsonify(payload)
    except WorkflowStudioAuthorityError as exc:
        return (
            jsonify(
                {
                    "error": exc.error_code,
                    "error_code": exc.error_code,
                }
            ),
            403,
        )
    except Exception as exc:
        logger.exception("Failed to build workflow studio catalogue")
        return (
            jsonify(
                {
                    "error": "workflow_studio_catalogue_failed",
                    "detail": str(exc),
                }
            ),
            500,
        )


def _workflow_studio_not_found_response(workflow_id: str):
    return (
        jsonify(
            {
                "error": "workflow_definition_not_found",
                "workflow_id": workflow_id,
            }
        ),
        404,
    )


def _workflow_studio_target_not_found(workflow_id: str):
    """Conceal an existing Studio target not visible to the request actor."""

    if not can_access_concept(workflow_id):
        return _workflow_studio_not_found_response(workflow_id)
    return None


def _workflow_studio_authority_response(
    exc: WorkflowStudioAuthorityError,
    workflow_id: str,
):
    """Return a bounded authentication denial or conceal an inaccessible graph."""

    if exc.error_code == "workflow_actor_authority_required":
        return (
            jsonify(
                {
                    "error": exc.error_code,
                    "error_code": exc.error_code,
                    "workflow_id": workflow_id,
                }
            ),
            403,
        )
    return _workflow_studio_not_found_response(workflow_id)


def _resolve_workflow_studio_mutation_actor(workflow_id: str):
    """Resolve canonical ambient authority before entering a Studio write route."""

    try:
        return require_workflow_studio_mutation_actor(), None
    except WorkflowStudioAuthorityError as exc:
        return None, _workflow_studio_authority_response(exc, workflow_id)


@workflows_bp.get("/api/workflow-studio/workflows/<path:workflow_id>")
def api_get_workflow_studio_workflow(workflow_id: str):
    target_not_found = _workflow_studio_target_not_found(workflow_id)
    if target_not_found is not None:
        return target_not_found

    namespace = request.args.get("namespace")
    session_id = request.args.get("session_id")
    turn_id = request.args.get("turn_id")
    try:
        payload = build_workflow_studio_detail_payload(
            workflow_id,
            namespace=namespace or None,
            session_id=session_id or None,
            turn_id=turn_id or None,
        )
        if not payload.get("raw") and not payload.get("views", {}).get("topology", {}).get(
            "definition"
        ):
            return (
                jsonify(
                    {
                        "error": "workflow_definition_not_found",
                        "workflow_id": workflow_id,
                    }
                ),
                404,
            )
        return jsonify(payload)
    except WorkflowStudioAuthorityError:
        return (
            jsonify(
                {
                    "error": "workflow_definition_not_found",
                    "workflow_id": workflow_id,
                }
            ),
            404,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc), "workflow_id": workflow_id}), 400
    except Exception as exc:
        logger.exception("Failed to build workflow studio payload for %s", workflow_id)
        return (
            jsonify(
                {
                    "error": "workflow_studio_detail_failed",
                    "workflow_id": workflow_id,
                    "detail": str(exc),
                }
            ),
            500,
        )


@workflows_bp.post("/api/workflow-studio/workflows/<path:workflow_id>/authoring/preview")
def api_preview_workflow_studio_authoring(workflow_id: str):
    payload = request.get_json(silent=True) or {}
    authoring_spec = payload.get("authoring_spec")
    base_definition_hash = payload.get("base_definition_hash")
    if not isinstance(authoring_spec, dict):
        return jsonify({"error": "authoring_spec_dict_required"}), 400
    try:
        return jsonify(
            preview_workflow_authoring_spec(
                workflow_id,
                authoring_spec=authoring_spec,
                base_definition_hash=(
                    str(base_definition_hash).strip()
                    if isinstance(base_definition_hash, str)
                    else None
                ),
            )
        )
    except WorkflowStudioConflictError as exc:
        return jsonify({"error": str(exc), "workflow_id": workflow_id}), 409
    except WorkflowStudioAuthorityError:
        return _workflow_studio_not_found_response(workflow_id)
    except ValueError as exc:
        return jsonify({"error": str(exc), "workflow_id": workflow_id}), 400
    except Exception as exc:
        logger.exception(
            "Workflow studio authoring preview failed for %s",
            workflow_id,
        )
        return (
            jsonify(
                {
                    "error": "workflow_studio_authoring_preview_failed",
                    "workflow_id": workflow_id,
                    "detail": str(exc),
                }
            ),
            500,
        )


@workflows_bp.post("/api/workflow-studio/workflows/<path:workflow_id>/authoring/apply")
def api_apply_workflow_studio_authoring(workflow_id: str):
    _actor_scope, actor_error_response = _resolve_workflow_studio_mutation_actor(
        workflow_id
    )
    if actor_error_response is not None:
        return actor_error_response
    payload = request.get_json(silent=True) or {}
    authoring_spec = payload.get("authoring_spec")
    base_definition_hash = payload.get("base_definition_hash")
    if not isinstance(authoring_spec, dict):
        return jsonify({"error": "authoring_spec_dict_required"}), 400
    try:
        result = apply_workflow_authoring_spec(
            workflow_id,
            authoring_spec=authoring_spec,
            base_definition_hash=(
                str(base_definition_hash).strip()
                if isinstance(base_definition_hash, str)
                else None
            ),
        )
        try:
            from ...workflows.durable.registry_factory import (
                invalidate_shared_workflow_registry_read_only,
            )

            invalidate_shared_workflow_registry_read_only()
        except Exception:
            logger.debug(
                "workflow studio apply could not invalidate shared workflow registry",
                exc_info=True,
            )
        _clear_workflow_definitions_cache()
        return jsonify(result)
    except WorkflowStudioConflictError as exc:
        return jsonify({"error": str(exc), "workflow_id": workflow_id}), 409
    except WorkflowStudioAuthorityError as exc:
        return _workflow_studio_authority_response(exc, workflow_id)
    except ValueError as exc:
        return jsonify({"error": str(exc), "workflow_id": workflow_id}), 400
    except Exception as exc:
        logger.exception("Workflow studio authoring apply failed for %s", workflow_id)
        return (
            jsonify(
                {
                    "error": "workflow_studio_authoring_apply_failed",
                    "workflow_id": workflow_id,
                    "detail": str(exc),
                }
            ),
            500,
        )


@workflows_bp.post("/api/workflow-studio/workflows/<path:workflow_id>/proposals/authoring")
def api_submit_workflow_studio_authoring_proposal(workflow_id: str):
    actor_scope, actor_error_response = _resolve_workflow_studio_mutation_actor(
        workflow_id
    )
    if actor_error_response is not None:
        return actor_error_response
    payload = request.get_json(silent=True) or {}
    authoring_spec = payload.get("authoring_spec")
    base_definition_hash = payload.get("base_definition_hash")
    if not isinstance(authoring_spec, dict):
        return jsonify({"error": "authoring_spec_dict_required"}), 400
    user_concept_id = actor_scope.user_concept_id
    namespace = _safe_request_arg(payload.get("namespace")) or None
    try:
        result = submit_workflow_authoring_proposal(
            workflow_id,
            authoring_spec=authoring_spec,
            base_definition_hash=(
                str(base_definition_hash).strip()
                if isinstance(base_definition_hash, str)
                else None
            ),
            session_id=_safe_request_arg(payload.get("session_id")) or None,
            turn_id=_safe_request_arg(payload.get("turn_id")) or None,
            proposed_by=user_concept_id,
        )
        result["namespace"] = namespace
        return jsonify(result)
    except WorkflowStudioConflictError as exc:
        return jsonify({"error": str(exc), "workflow_id": workflow_id}), 409
    except WorkflowStudioAuthorityError as exc:
        return _workflow_studio_authority_response(exc, workflow_id)
    except ValueError as exc:
        return jsonify({"error": str(exc), "workflow_id": workflow_id}), 400
    except Exception as exc:
        logger.exception(
            "Workflow studio authoring proposal submit failed for %s",
            workflow_id,
        )
        return (
            jsonify(
                {
                    "error": "workflow_studio_authoring_proposal_submit_failed",
                    "workflow_id": workflow_id,
                    "detail": str(exc),
                }
            ),
            500,
        )


@workflows_bp.post("/api/workflow-studio/workflows/<path:workflow_id>/proposals/review")
def api_review_workflow_studio_authoring_proposal(workflow_id: str):
    target_not_found = _workflow_studio_target_not_found(workflow_id)
    if target_not_found is not None:
        return target_not_found
    actor_scope, actor_error_response = _resolve_workflow_studio_mutation_actor(
        workflow_id
    )
    if actor_error_response is not None:
        return actor_error_response
    payload = request.get_json(silent=True) or {}
    action = _safe_request_arg(payload.get("action"))
    review_reason = _safe_request_arg(payload.get("review_reason")) or None
    namespace = _safe_request_arg(payload.get("namespace")) or None
    user_concept_id = actor_scope.user_concept_id
    org_concept_id = actor_scope.organisation_concept_id
    if not action:
        return jsonify({"error": "review_action_required", "workflow_id": workflow_id}), 400
    try:
        result = review_workflow_authoring_proposal(
            workflow_id,
            action=action,
            review_reason=review_reason,
            reviewed_by=user_concept_id,
            user_id=user_concept_id,
            org_id=org_concept_id,
            namespace=namespace,
        )
        try:
            from ...workflows.durable.registry_factory import (
                invalidate_shared_workflow_registry_read_only,
            )

            invalidate_shared_workflow_registry_read_only()
        except Exception:
            logger.debug(
                "workflow studio proposal review could not invalidate shared workflow registry",
                exc_info=True,
            )
        _clear_workflow_definitions_cache()
        return jsonify(result)
    except WorkflowStudioConflictError as exc:
        return jsonify({"error": str(exc), "workflow_id": workflow_id}), 409
    except WorkflowStudioAuthorityError as exc:
        return _workflow_studio_authority_response(exc, workflow_id)
    except ValueError as exc:
        return jsonify({"error": str(exc), "workflow_id": workflow_id}), 400
    except Exception as exc:
        logger.exception(
            "Workflow studio authoring proposal review failed for %s",
            workflow_id,
        )
        return (
            jsonify(
                {
                    "error": "workflow_studio_authoring_proposal_review_failed",
                    "workflow_id": workflow_id,
                    "detail": str(exc),
                }
            ),
            500,
        )


@workflows_bp.post("/api/workflow-studio/workflows/<path:workflow_id>/proposals/rollback")
def api_rollback_workflow_studio_authoring_promotion(workflow_id: str):
    target_not_found = _workflow_studio_target_not_found(workflow_id)
    if target_not_found is not None:
        return target_not_found
    actor_scope, actor_error_response = _resolve_workflow_studio_mutation_actor(
        workflow_id
    )
    if actor_error_response is not None:
        return actor_error_response
    payload = request.get_json(silent=True) or {}
    review_reason = _safe_request_arg(payload.get("review_reason")) or None
    namespace = _safe_request_arg(payload.get("namespace")) or None
    user_concept_id = actor_scope.user_concept_id
    org_concept_id = actor_scope.organisation_concept_id
    try:
        result = rollback_workflow_authoring_promotion(
            workflow_id,
            review_reason=review_reason,
            reviewed_by=user_concept_id,
            user_id=user_concept_id,
            org_id=org_concept_id,
            namespace=namespace,
        )
        try:
            from ...workflows.durable.registry_factory import (
                invalidate_shared_workflow_registry_read_only,
            )

            invalidate_shared_workflow_registry_read_only()
        except Exception:
            logger.debug(
                "workflow studio promotion rollback could not invalidate shared workflow registry",
                exc_info=True,
            )
        _clear_workflow_definitions_cache()
        return jsonify(result)
    except WorkflowStudioConflictError as exc:
        return jsonify({"error": str(exc), "workflow_id": workflow_id}), 409
    except WorkflowStudioAuthorityError as exc:
        return _workflow_studio_authority_response(exc, workflow_id)
    except ValueError as exc:
        return jsonify({"error": str(exc), "workflow_id": workflow_id}), 400
    except Exception as exc:
        logger.exception(
            "Workflow studio promotion rollback failed for %s",
            workflow_id,
        )
        return (
            jsonify(
                {
                    "error": "workflow_studio_authoring_promotion_rollback_failed",
                    "workflow_id": workflow_id,
                    "detail": str(exc),
                }
            ),
            500,
        )


@workflows_bp.post("/api/workflow-studio/workflows/<path:workflow_id>/publication/demote")
def api_demote_workflow_studio_publication(workflow_id: str):
    target_not_found = _workflow_studio_target_not_found(workflow_id)
    if target_not_found is not None:
        return target_not_found
    actor_scope, actor_error_response = _resolve_workflow_studio_mutation_actor(
        workflow_id
    )
    if actor_error_response is not None:
        return actor_error_response
    payload = request.get_json(silent=True) or {}
    review_reason = _safe_request_arg(payload.get("review_reason")) or None
    user_concept_id = actor_scope.user_concept_id
    try:
        result = demote_workflow_routing(
            workflow_id,
            review_reason=review_reason,
            reviewed_by=user_concept_id,
        )
        _clear_workflow_definitions_cache()
        return jsonify(result)
    except WorkflowStudioAuthorityError as exc:
        return _workflow_studio_authority_response(exc, workflow_id)
    except ValueError as exc:
        return jsonify({"error": str(exc), "workflow_id": workflow_id}), 400
    except Exception as exc:
        logger.exception("Workflow studio demotion failed for %s", workflow_id)
        return (
            jsonify(
                {
                    "error": "workflow_studio_publication_demote_failed",
                    "workflow_id": workflow_id,
                    "detail": str(exc),
                }
            ),
            500,
        )


@workflows_bp.post("/api/workflow-studio/workflows/<path:workflow_id>/publication/supersede")
def api_supersede_workflow_studio_publication(workflow_id: str):
    target_not_found = _workflow_studio_target_not_found(workflow_id)
    if target_not_found is not None:
        return target_not_found
    actor_scope, actor_error_response = _resolve_workflow_studio_mutation_actor(
        workflow_id
    )
    if actor_error_response is not None:
        return actor_error_response
    payload = request.get_json(silent=True) or {}
    replacement_workflow_id = _safe_request_arg(payload.get("replacement_workflow_id"))
    review_reason = _safe_request_arg(payload.get("review_reason")) or None
    user_concept_id = actor_scope.user_concept_id
    if not replacement_workflow_id:
        return (
            jsonify(
                {
                    "error": "replacement_workflow_id_required",
                    "workflow_id": workflow_id,
                }
            ),
            400,
        )
    replacement_not_found = _workflow_studio_target_not_found(
        replacement_workflow_id
    )
    if replacement_not_found is not None:
        return replacement_not_found
    try:
        result = supersede_workflow_publication(
            workflow_id,
            replacement_workflow_id=replacement_workflow_id,
            review_reason=review_reason,
            reviewed_by=user_concept_id,
        )
        _clear_workflow_definitions_cache()
        return jsonify(result)
    except WorkflowStudioAuthorityError as exc:
        return _workflow_studio_authority_response(exc, workflow_id)
    except ValueError as exc:
        return jsonify({"error": str(exc), "workflow_id": workflow_id}), 400
    except Exception as exc:
        logger.exception("Workflow studio supersession failed for %s", workflow_id)
        return (
            jsonify(
                {
                    "error": "workflow_studio_publication_supersede_failed",
                    "workflow_id": workflow_id,
                    "detail": str(exc),
                }
            ),
            500,
        )


@workflows_bp.post("/api/workflow-studio/workflows/<path:workflow_id>/proposals/description")
def api_build_workflow_studio_description_proposal(workflow_id: str):
    target_not_found = _workflow_studio_target_not_found(workflow_id)
    if target_not_found is not None:
        return target_not_found
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode") or "auto")
    user_concept_id, org_concept_id = _resolve_request_llm_scope_ids()
    try:
        return jsonify(
            build_workflow_description_proposal(
                workflow_id,
                mode=mode,
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
            )
        )
    except ValueError as exc:
        return jsonify({"error": str(exc), "workflow_id": workflow_id}), 400
    except Exception as exc:
        logger.exception(
            "Workflow studio description proposal failed for %s",
            workflow_id,
        )
        return (
            jsonify(
                {
                    "error": "workflow_studio_description_proposal_failed",
                    "workflow_id": workflow_id,
                    "detail": str(exc),
                }
            ),
            500,
        )


@workflows_bp.get("/api/workflows/executions/<execution_id>")
def api_get_workflow_execution(execution_id: str):
    doc = get_workflow_execution_trace(execution_id)
    workflow_id = doc.get("workflow_id") if isinstance(doc, dict) else None
    workflow_visible = bool(
        isinstance(workflow_id, str)
        and workflow_id
        in filter_workflow_ids_for_current_actor([workflow_id])
    )
    if (
        not doc
        or not workflow_visible
        or not _workflow_trace_matches_current_actor(doc)
    ):
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
    actor_scope, actor_error_response = _resolve_http_workflow_actor_scope(
        claimed_user_id=request.args.get("user_id"),
        claimed_org_id=request.args.get("org_id"),
        claimed_namespace=request.args.get("namespace"),
    )
    if actor_error_response is not None:
        return actor_error_response
    namespace = getattr(actor_scope, "namespace", None)
    workflow_id = _safe_request_arg(request.args.get("workflow_id")) or None
    if workflow_id and workflow_id not in filter_workflow_ids_for_current_actor(
        [workflow_id]
    ):
        return jsonify({"items": [], "count": 0})

    docs = list_recent_workflow_execution_traces(
        limit=limit,
        namespace=namespace,
        workflow_id=workflow_id,
    )
    visible_workflow_ids = set(
        filter_workflow_ids_for_current_actor(
            [
                doc.get("workflow_id")
                for doc in docs
                if isinstance(doc, dict)
            ]
        )
    )
    docs = [
        doc
        for doc in docs
        if isinstance(doc, dict)
        and doc.get("workflow_id") in visible_workflow_ids
        and _workflow_trace_matches_current_actor(doc)
    ]
    return jsonify({"items": docs, "count": len(docs)})


@workflows_bp.get("/api/workflows/predictions/envelope")
def api_get_workflow_prediction_envelope():
    workflow_id = _safe_request_arg(request.args.get("workflow_id"))
    if not workflow_id:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "missing_workflow_id",
                    "detail": "workflow_id is required",
                }
            ),
            400,
        )

    actor_scope, actor_error_response = _resolve_http_workflow_actor_scope(
        claimed_user_id=request.args.get("user_id"),
        claimed_org_id=request.args.get("org_id"),
        claimed_namespace=request.args.get("namespace"),
    )
    if actor_error_response is not None:
        return actor_error_response
    if workflow_id not in filter_workflow_ids_for_current_actor([workflow_id]):
        return (
            jsonify(
                {
                    "success": False,
                    "error": "workflow_not_found",
                    "workflow_id": workflow_id,
                }
            ),
            404,
        )
    namespace = getattr(actor_scope, "namespace", None)
    model = _safe_request_arg(request.args.get("model")) or None
    provider = _safe_request_arg(request.args.get("provider")) or None
    limit_raw = request.args.get("limit", "50")
    try:
        limit = int(limit_raw)
    except Exception:
        limit = 50

    try:
        payload = build_workflow_prediction_envelope(
            workflow_id=workflow_id,
            namespace=namespace,
            model=model,
            provider=provider,
            limit=limit,
        )
    except ValueError as exc:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "invalid_prediction_request",
                    "detail": str(exc),
                }
            ),
            400,
        )
    except Exception as exc:
        logger.exception(
            "Workflow prediction envelope build failed for %s",
            workflow_id,
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": "workflow_prediction_envelope_failed",
                    "workflow_id": workflow_id,
                    "detail": str(exc),
                }
            ),
            500,
        )

    return jsonify(payload)


@workflows_bp.get("/api/workflows/episodes")
def api_list_workflow_use_episodes():
    """List workflow-use episodes for monitor drill-down diagnostics."""

    workflow_id = _safe_request_arg(request.args.get("workflow_id")) or None
    actor_scope, actor_error_response = _resolve_http_workflow_actor_scope(
        claimed_user_id=request.args.get("user_id"),
        claimed_org_id=request.args.get("org_id"),
        claimed_namespace=request.args.get("namespace"),
    )
    if actor_error_response is not None:
        return actor_error_response
    namespace = getattr(actor_scope, "namespace", None)
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
        from ...workflows.durable.registry_factory import (
            build_durable_workflow_registry_read_only,
        )

        registry = build_durable_workflow_registry_read_only(
            defer_parity_work=True
        )
        visible_workflow_ids = filter_workflow_ids_for_current_actor(
            registry.all_workflow_ids()
        )
        if workflow_id:
            visible_workflow_ids = [
                candidate
                for candidate in visible_workflow_ids
                if candidate == workflow_id
            ]
        items = list_workflow_use_episodes(
            workflow_id=workflow_id,
            workflow_ids=visible_workflow_ids,
            namespace=namespace,
            session_id=session_id or None,
            turn_id=turn_id or None,
            strict_namespace_scope=True,
            limit=limit,
        )
        total = count_workflow_use_episodes(
            workflow_id=workflow_id,
            workflow_ids=visible_workflow_ids,
            namespace=namespace,
            session_id=session_id or None,
            turn_id=turn_id or None,
            strict_namespace_scope=True,
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


def _resolve_http_workflow_actor_scope(
    *,
    claimed_user_id: Any = None,
    claimed_org_id: Any = None,
    claimed_namespace: Any = None,
):
    """Resolve request actor authority without trusting HTTP payload claims."""

    from ...services.workflow_actor_scope_service import (
        WorkflowActorScopeError,
        resolve_authoritative_workflow_actor_scope,
    )

    try:
        return (
            resolve_authoritative_workflow_actor_scope(
                claimed_user_id=claimed_user_id,
                claimed_org_id=claimed_org_id,
                claimed_namespace=claimed_namespace,
                allow_unscoped_claims=False,
            ),
            None,
        )
    except WorkflowActorScopeError as exc:
        status_code = (
            400 if exc.reason == "workflow_actor_namespace_invalid" else 403
        )
        return (
            None,
            (
                jsonify(
                    {
                        "error": exc.reason,
                        "error_code": exc.reason,
                        "mismatch_fields": list(exc.mismatch_fields),
                    }
                ),
                status_code,
            ),
        )


def _workflow_schedule_matches_current_actor(schedule: Any) -> bool:
    """Return whether the persisted schedule belongs to the request actor."""

    actor_scope = _match_exact_persisted_workflow_actor(
        user_id=getattr(schedule, "user_id", None),
        org_id=getattr(schedule, "org_id", None),
        namespace=getattr(schedule, "namespace", None),
    )
    workflow_id = getattr(schedule, "workflow_id", None)
    return bool(
        actor_scope is not None
        and isinstance(workflow_id, str)
        and workflow_id in filter_workflow_ids_for_current_actor([workflow_id])
    )


def _workflow_instance_matches_current_actor(instance: Any) -> bool:
    """Return whether the persisted instance belongs to the request actor."""

    actor_scope = _match_exact_persisted_workflow_actor(
        user_id=getattr(instance, "user_id", None),
        org_id=getattr(instance, "org_id", None),
        namespace=getattr(instance, "namespace", None),
    )
    workflow_id = getattr(instance, "workflow_id", None)
    return bool(
        actor_scope is not None
        and isinstance(workflow_id, str)
        and workflow_id in filter_workflow_ids_for_current_actor([workflow_id])
    )


def _match_exact_persisted_workflow_actor(
    *,
    user_id: Any,
    org_id: Any,
    namespace: Any,
):
    """Return current actor scope only for a complete, canonical owner envelope.

    Legacy rows with an omitted organisation or namespace cannot prove which
    current tenant owns them. They remain available to trusted migration tools,
    but actor-facing direct-ID routes conceal them.
    """

    actor_scope, _error_response = _resolve_http_workflow_actor_scope(
        claimed_user_id=user_id,
        claimed_org_id=org_id,
        claimed_namespace=namespace,
    )
    if actor_scope is None:
        return None

    def _normalise_concept_id(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        cleaned = value.strip()
        return cleaned if cleaned.startswith("#") else f"#V#{cleaned}"

    persisted_user_id = _normalise_concept_id(user_id)
    persisted_org_id = _normalise_concept_id(org_id)
    persisted_namespace = (
        namespace.strip()
        if isinstance(namespace, str) and namespace.strip()
        else None
    )
    if persisted_user_id != actor_scope.user_concept_id:
        return None
    if persisted_org_id != actor_scope.organisation_concept_id:
        return None
    if persisted_namespace != actor_scope.namespace:
        return None
    return actor_scope


def _workflow_trace_matches_current_actor(trace: Any) -> bool:
    """Return whether a persisted trace belongs exactly to the request actor."""

    if not isinstance(trace, dict):
        return False
    trace_namespace = trace.get("user_namespace") or trace.get("namespace")
    trace_user_id = trace.get("user_id") or trace.get("user_concept_id")
    trace_org_id = trace.get("org_id") or trace.get("organisation_concept_id")
    actor_scope, _error_response = _resolve_http_workflow_actor_scope(
        claimed_user_id=trace_user_id,
        claimed_org_id=trace_org_id,
        claimed_namespace=trace_namespace,
    )
    if actor_scope is None:
        return False

    from ...services.namespace_service import derive_actor_context_from_namespace

    namespace_user_id, namespace_org_id = derive_actor_context_from_namespace(
        trace_namespace
    )
    persisted_user_id = trace_user_id or namespace_user_id
    persisted_org_id = trace_org_id or namespace_org_id
    if (
        actor_scope.user_concept_id
        and persisted_user_id != actor_scope.user_concept_id
    ):
        return False
    if (
        actor_scope.organisation_concept_id
        and persisted_org_id != actor_scope.organisation_concept_id
    ):
        # Legacy traces without an organisation cannot prove which current
        # organisation owns them; conceal them rather than crossing cohorts.
        return False
    return True


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

    from ...services.workflow_actor_scope_service import (
        WorkflowActorScopeError,
        resolve_authoritative_workflow_actor_scope,
    )

    try:
        actor_scope = resolve_authoritative_workflow_actor_scope(
            claimed_user_id=data.get("user_id"),
            claimed_org_id=data.get("org_id"),
            claimed_namespace=data.get("namespace"),
            allow_unscoped_claims=False,
        )
    except WorkflowActorScopeError as exc:
        status_code = (
            400
            if exc.reason == "workflow_actor_namespace_invalid"
            else 403
        )
        return (
            jsonify(
                {
                    "error": exc.reason,
                    "error_code": exc.reason,
                    "mismatch_fields": list(exc.mismatch_fields),
                }
            ),
            status_code,
        )
    user_id = actor_scope.user_concept_id
    org_id = actor_scope.organisation_concept_id
    namespace = actor_scope.namespace
    if not user_id or not namespace:
        return (
            jsonify(
                {
                    "error": "workflow_actor_authority_required",
                    "error_code": "workflow_actor_authority_required",
                }
            ),
            403,
        )
    if workflow_id not in filter_workflow_ids_for_current_actor([workflow_id]):
        return jsonify({"error": "workflow_not_found"}), 404
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
    - status: Comma-separated statuses (pending, running, completed, failed, cancelled, paused)
    - workflow_id: Filter by workflow definition
    - source_event_type: Filter by triggering event type
    - source_event_id: Filter by triggering event ID
    - limit: Maximum results (default 50)
    """
    claimed_user_id = request.args.get("user_id")
    claimed_org_id = request.args.get("org_id")
    claimed_namespace = request.args.get("namespace")
    status_str = request.args.get("status")
    workflow_id = request.args.get("workflow_id")
    source_event_type = request.args.get("source_event_type")
    source_event_id = request.args.get("source_event_id")
    limit = min(int(request.args.get("limit", "50")), 200)

    try:
        statuses = _parse_workflow_instance_status_filters(status_str)
    except ValueError:
        valid_statuses = ", ".join(status.value for status in WorkflowInstanceStatus)
        return (
            jsonify(
                {
                    "error": f"Invalid status: {status_str}. Valid values: {valid_statuses}"
                }
            ),
            400,
        )

    actor_scope, actor_error_response = _resolve_http_workflow_actor_scope(
        claimed_user_id=claimed_user_id,
        claimed_org_id=claimed_org_id,
        claimed_namespace=claimed_namespace,
    )
    if actor_error_response is not None:
        return actor_error_response
    user_id = getattr(actor_scope, "user_concept_id", None)
    org_id = getattr(actor_scope, "organisation_concept_id", None)
    namespace = getattr(actor_scope, "namespace", None)
    if workflow_id and workflow_id not in filter_workflow_ids_for_current_actor(
        [workflow_id]
    ):
        return jsonify({"items": [], "count": 0})

    manager = _get_instance_manager()
    try:
        items = manager.list_instance_status_dicts(
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            status=statuses,
            workflow_id=workflow_id,
            source_event_type=source_event_type,
            source_event_id=source_event_id,
            limit=limit,
        )
        visible_workflow_ids = set(
            filter_workflow_ids_for_current_actor(
                [
                    item.get("workflow_id")
                    for item in items
                    if isinstance(item, dict)
                ]
            )
        )
        items = [
            item
            for item in items
            if isinstance(item, dict)
            and item.get("workflow_id") in visible_workflow_ids
        ]
    except Exception as exc:
        if _is_transient_workflow_instances_error(exc):
            logger.warning(
                "Workflow instances snapshot degraded due to transient store error: %s",
                exc,
                exc_info=True,
            )
            return _build_retryable_workflow_instances_response(
                error="Workflow monitor temporarily unavailable; please retry.",
                detail=str(exc),
                payload={
                    "items": [],
                    "count": 0,
                },
            )
        logger.exception("Failed to list workflow instances")
        return jsonify({"error": str(exc)}), 500

    return jsonify(
        {
            "items": items,
            "count": len(items),
        }
    )


@workflows_bp.get("/api/workflows/instances/<instance_id>")
def api_get_workflow_instance(instance_id: str):
    """Get details of a specific workflow instance."""
    manager = _get_instance_manager()
    try:
        instance = manager.get_instance(instance_id)
    except Exception as exc:
        if _is_transient_workflow_instances_error(exc):
            logger.warning(
                "Workflow instance status degraded due to transient store error: %s",
                exc,
                exc_info=True,
            )
            return _build_retryable_workflow_instances_response(
                error="Workflow instance temporarily unavailable; please retry.",
                detail=str(exc),
                payload={
                    "instance_id": instance_id,
                },
            )
        logger.exception("Failed to get workflow instance")
        return jsonify({"error": str(exc)}), 500

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
    if not _workflow_instance_matches_current_actor(instance):
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

    try:
        instance = manager.get_instance(instance_id)
    except Exception as exc:
        if _is_transient_workflow_instances_error(exc):
            logger.warning(
                "Workflow cancellation status degraded due to transient store error: %s",
                exc,
                exc_info=True,
            )
            return _build_retryable_workflow_instances_response(
                error="Workflow cancellation temporarily unavailable; please retry.",
                detail=str(exc),
                payload={"instance_id": instance_id, "operation": "cancel"},
            )
        logger.exception("Failed to authorise workflow cancellation")
        return jsonify({"error": str(exc)}), 500
    if not instance or not _workflow_instance_matches_current_actor(instance):
        return (
            jsonify({"error": "instance_not_found", "instance_id": instance_id}),
            404,
        )

    try:
        success = manager.mark_cancelled(instance_id)
    except Exception as exc:
        if _is_transient_workflow_instances_error(exc):
            logger.warning(
                "Workflow cancel degraded due to transient store error: %s",
                exc,
                exc_info=True,
            )
            return _build_retryable_workflow_instances_response(
                error="Workflow cancellation temporarily unavailable; please retry.",
                detail=str(exc),
                payload={
                    "instance_id": instance_id,
                    "operation": "cancel",
                },
            )
        logger.exception("Failed to cancel workflow instance")
        return jsonify({"error": str(exc)}), 500
    if success:
        return jsonify({"status": "cancelled", "instance_id": instance_id})

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

    return jsonify({"error": "cancel_failed"}), 500


@workflows_bp.post("/api/workflows/instances/<instance_id>/retry")
def api_retry_workflow_instance(instance_id: str):
    """Reset a failed workflow instance for retry."""
    manager = _get_instance_manager()

    # Verify instance exists
    try:
        instance = manager.get_instance(instance_id)
    except Exception as exc:
        if _is_transient_workflow_instances_error(exc):
            logger.warning(
                "Workflow retry degraded due to transient store error: %s",
                exc,
            )
            return _build_retryable_workflow_instances_response(
                error="Workflow retry temporarily unavailable; please retry.",
                detail=str(exc),
                payload={"instance_id": instance_id},
            )
        raise
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
    if not _workflow_instance_matches_current_actor(instance):
        return (
            jsonify({"error": "instance_not_found", "instance_id": instance_id}),
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

    try:
        success = manager.reset_for_retry(instance_id)
    except Exception as exc:
        if _is_transient_workflow_instances_error(exc):
            logger.warning(
                "Workflow retry reset degraded due to transient store error: %s",
                exc,
            )
            return _build_retryable_workflow_instances_response(
                error="Workflow retry temporarily unavailable; please retry.",
                detail=str(exc),
                payload={"instance_id": instance_id},
            )
        raise
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
    if not _workflow_instance_matches_current_actor(instance):
        return (
            jsonify({"error": "instance_not_found", "instance_id": instance_id}),
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

        actor_scope, actor_error_response = _resolve_http_workflow_actor_scope(
            claimed_user_id=request.args.get("user_id"),
            claimed_org_id=request.args.get("org_id"),
            claimed_namespace=request.args.get("namespace"),
        )
        if actor_error_response is not None:
            return actor_error_response
        user_id = getattr(actor_scope, "user_concept_id", None)
        org_id = getattr(actor_scope, "organisation_concept_id", None)
        namespace = getattr(actor_scope, "namespace", None)
        workflow_id = request.args.get("workflow_id")
        instance_id = request.args.get("instance_id")
        status_raw = request.args.get("status")

        if workflow_id:
            visible_workflow_ids = set(
                filter_workflow_ids_for_current_actor([workflow_id])
            )
            if workflow_id not in visible_workflow_ids:
                return jsonify({"error": "workflow_not_found"}), 404
        else:
            from ...workflows.durable.registry_factory import (
                build_durable_workflow_registry_read_only,
            )

            registry = build_durable_workflow_registry_read_only(
                defer_parity_work=True
            )
            visible_workflow_ids = set(
                filter_workflow_ids_for_current_actor(
                    registry.all_workflow_ids()
                )
            )

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
            allowed_workflow_ids=visible_workflow_ids,
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

    actor_scope, actor_error_response = _resolve_http_workflow_actor_scope(
        claimed_user_id=data.get("user_id"),
        claimed_org_id=data.get("org_id"),
        claimed_namespace=data.get("namespace"),
    )
    if actor_error_response is not None:
        return actor_error_response
    user_id = getattr(actor_scope, "user_concept_id", None)
    org_id = getattr(actor_scope, "organisation_concept_id", None)
    namespace = getattr(actor_scope, "namespace", None)
    if not user_id or not namespace:
        return (
            jsonify(
                {
                    "error": "workflow_actor_authority_required",
                    "error_code": "workflow_actor_authority_required",
                }
            ),
            403,
        )
    if workflow_id not in filter_workflow_ids_for_current_actor([workflow_id]):
        return jsonify({"error": "workflow_not_found"}), 404
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
    actor_scope, actor_error_response = _resolve_http_workflow_actor_scope(
        claimed_user_id=request.args.get("user_id"),
    )
    if actor_error_response is not None:
        return actor_error_response
    user_id = getattr(actor_scope, "user_concept_id", None)
    enabled_only = request.args.get("enabled_only", "false").lower() == "true"
    limit = min(int(request.args.get("limit", "50")), 200)

    manager = _get_instance_manager()
    schedules = manager.list_schedules(
        user_id=user_id,
        enabled_only=enabled_only,
        limit=limit,
    )
    schedules = [
        schedule
        for schedule in schedules
        if _workflow_schedule_matches_current_actor(schedule)
    ]

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
    if not _workflow_schedule_matches_current_actor(schedule):
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
    if not _workflow_schedule_matches_current_actor(schedule):
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
    if not _workflow_schedule_matches_current_actor(schedule):
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
    if not _workflow_schedule_matches_current_actor(schedule):
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
