"""Event-driven workflow launch helpers (WS6 / JVNAUTOSCI-1090).

This module is the canonical pathway from domain events to durable workflow
instances. Keep event-to-workflow launch logic here rather than duplicating
launch code across routes, MCP handlers, or services.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from .feature_flags import (
    get_durable_workflows_enabled,
    get_event_workflow_integration_enabled,
)
from ..workflows.durable.models import EventWorkflowBinding
from ..workflows.durable.startup import get_instance_manager

logger = logging.getLogger(__name__)

# Canonical event type labels for traceability.
EVENT_TYPE_TASK_CREATED = "task.created"
EVENT_TYPE_TASK_STATUS_CHANGED = "task.status_changed"
EVENT_TYPE_DIRECT_MESSAGE_CREATED = "message.direct_created"
EVENT_TYPE_TYPE_CREATED = "type.created"
EVENT_TYPE_CONCEPT_CREATED = "concept.created"
EVENT_TYPE_CONCEPT_UPDATED = "concept.updated"
EVENT_TYPE_CONCEPT_DELETED = "concept.deleted"
EVENT_TYPE_RELATIONSHIP_ADDED = "relationship.added"
EVENT_TYPE_RELATIONSHIP_REMOVED = "relationship.removed"
EVENT_TYPE_TEXT_RELATION_UPSERTED = "text_relation.upserted"
EVENT_TYPE_TEXT_RELATION_UPDATED = "text_relation.updated"
EVENT_TYPE_TEXT_RELATION_DELETED = "text_relation.deleted"
EVENT_TYPE_VONTOLOGY_MUTATED = "vontology.mutated"

# Backward-compatible environment wiring (legacy/system bootstrap).
EVENT_WORKFLOW_ID_ENV_MAP: dict[str, str] = {
    EVENT_TYPE_TASK_CREATED: "VON_EVENT_TASK_CREATED_WORKFLOW_ID",
    EVENT_TYPE_TASK_STATUS_CHANGED: "VON_EVENT_TASK_STATUS_CHANGED_WORKFLOW_ID",
    EVENT_TYPE_DIRECT_MESSAGE_CREATED: "VON_EVENT_DIRECT_MESSAGE_WORKFLOW_ID",
    EVENT_TYPE_TYPE_CREATED: "VON_EVENT_TYPE_CREATED_WORKFLOW_ID",
}

_DEFAULT_EVENT_BINDINGS: tuple[dict[str, Any], ...] = (
    {
        "event_type": EVENT_TYPE_TYPE_CREATED,
        "workflow_id": "#V#salient_predicate_governance_workflow",
        "input_mapping": {"type_concept_id": "event.concept_id"},
        "enabled": True,
    },
)

_DEFAULT_BINDINGS_ENSURED = False


def _task_status_trigger_values() -> set[str]:
    raw = os.getenv(
        "VON_EVENT_TASK_STATUS_TRIGGER_VALUES",
        "in_progress,completed,blocked,cancelled",
    )
    values = {item.strip().lower() for item in raw.split(",") if item.strip()}
    if values:
        return values
    return {"in_progress", "completed", "blocked", "cancelled"}


def _workflow_id_for_event(event_type: str) -> str | None:
    env_name = EVENT_WORKFLOW_ID_ENV_MAP.get(event_type)
    if not env_name:
        return None

    workflow_id = os.getenv(env_name, "").strip()
    return workflow_id or None


def _build_event_idempotency_key(
    *,
    workflow_id: str,
    event_type: str,
    event_id: str,
) -> str:
    # Keep keys short/stable for indexed lookup and diagnostic readability.
    raw = f"{workflow_id}|{event_type}|{event_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"evt:{event_type}:{digest}"


def _build_namespace(user_id: str | None, org_id: str | None) -> str:
    safe_user = (user_id or "anonymous").strip() or "anonymous"
    safe_org = (org_id or "").strip()

    # Prefer user-only namespace when org context is unknown.
    # Historical user/default placeholders made monitor scoping opaque and
    # prevented reliable namespace filtering once org-scoped namespaces landed.
    if safe_org:
        return f"{safe_user}/{safe_org}"
    if safe_user != "anonymous":
        return safe_user
    return "anonymous/default"


def _normalise_namespace_override(namespace: str | None) -> str | None:
    if not isinstance(namespace, str):
        return None
    cleaned = namespace.strip()
    return cleaned or None


def _derive_actor_context_from_namespace(
    namespace: str | None,
) -> tuple[str | None, str | None]:
    """Infer user/org hints from a namespace string when available.

    Supported forms:
    - ``#V#user``
    - ``#V#user@org`` (org may optionally include ``#V#``)
    - ``user/org`` or ``user@org`` (legacy/internal forms)
    """

    namespace_clean = _normalise_namespace_override(namespace)
    if namespace_clean is None:
        return None, None

    if namespace_clean.startswith("#V#"):
        body = namespace_clean[3:]
        if "@" in body:
            user_raw, org_raw = body.split("@", 1)
            user_clean = user_raw.strip()
            org_clean = org_raw.strip()
            user_value = f"#V#{user_clean}" if user_clean else None
            if not org_clean:
                return user_value, None
            org_value = org_clean if org_clean.startswith("#V#") else f"#V#{org_clean}"
            return user_value, org_value
        return namespace_clean, None

    if "@" in namespace_clean:
        user_raw, org_raw = namespace_clean.split("@", 1)
        user_value = user_raw.strip() or None
        org_value = org_raw.strip() or None
        return user_value, org_value

    if "/" in namespace_clean:
        user_raw, org_raw = namespace_clean.split("/", 1)
        user_value = user_raw.strip() or None
        org_value = org_raw.strip() or None
        return user_value, org_value

    return namespace_clean, None


def resolve_event_actor_context(
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    namespace: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve actor context for event emission in request/non-request code paths."""

    resolved_user = (
        user_id.strip() if isinstance(user_id, str) and user_id.strip() else None
    )
    resolved_org = org_id.strip() if isinstance(org_id, str) and org_id.strip() else None
    namespace_user, namespace_org = _derive_actor_context_from_namespace(namespace)
    if resolved_user is None and namespace_user is not None:
        resolved_user = namespace_user
    if resolved_org is None and namespace_org is not None:
        resolved_org = namespace_org

    if resolved_user is None:
        try:
            from ..security.access_control import get_effective_user_concept_id

            actor = get_effective_user_concept_id()
            if isinstance(actor, str) and actor.strip():
                resolved_user = actor.strip()
        except Exception:
            resolved_user = None

    if resolved_org is None:
        try:
            from flask import has_request_context, session as flask_session

            if has_request_context():
                org_raw = flask_session.get("organisation_concept_id")
                if isinstance(org_raw, str) and org_raw.strip():
                    resolved_org = org_raw.strip()
        except Exception:
            resolved_org = None

    return resolved_user, resolved_org


def _normalise_event_binding_mapping(
    input_mapping: dict[str, Any] | None,
) -> dict[str, str]:
    normalised: dict[str, str] = {}
    if not isinstance(input_mapping, dict):
        return normalised
    for key, value in input_mapping.items():
        key_clean = str(key or "").strip()
        value_clean = str(value or "").strip()
        if key_clean and value_clean:
            normalised[key_clean] = value_clean
    return normalised


def ensure_default_event_bindings() -> dict[str, Any]:
    """Best-effort bootstrap of default event bindings.

    This is intentionally idempotent and conflict-safe:
    - Existing equivalent bindings are treated as unchanged.
    - Existing conflicting bindings are left untouched (no override).
    """

    global _DEFAULT_BINDINGS_ENSURED
    if _DEFAULT_BINDINGS_ENSURED:
        return {
            "success": True,
            "ensured": True,
            "created_count": 0,
            "updated_count": 0,
            "unchanged_count": 0,
            "skipped_conflicts": 0,
        }

    if os.getenv("VON_EVENT_BINDINGS_BOOTSTRAP_ENABLE", "1").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        _DEFAULT_BINDINGS_ENSURED = True
        return {
            "success": True,
            "ensured": False,
            "reason": "bootstrap_disabled",
            "created_count": 0,
            "updated_count": 0,
            "unchanged_count": 0,
            "skipped_conflicts": 0,
        }

    created_count = 0
    updated_count = 0
    unchanged_count = 0
    skipped_conflicts = 0
    errors: list[str] = []

    try:
        manager = get_instance_manager()
        if not hasattr(manager, "upsert_event_binding"):
            _DEFAULT_BINDINGS_ENSURED = True
            return {
                "success": False,
                "ensured": False,
                "reason": "event_binding_persistence_unavailable",
                "created_count": 0,
                "updated_count": 0,
                "unchanged_count": 0,
                "skipped_conflicts": 0,
            }

        for binding in _DEFAULT_EVENT_BINDINGS:
            try:
                _saved_binding, created, updated = manager.upsert_event_binding(
                    event_type=binding["event_type"],
                    workflow_id=binding["workflow_id"],
                    input_mapping=binding.get("input_mapping"),
                    enabled=bool(binding.get("enabled", True)),
                    actor="system.bootstrap",
                    replace_existing=False,
                )
                if created:
                    created_count += 1
                elif updated:
                    updated_count += 1
                else:
                    unchanged_count += 1
            except ValueError as exc:
                if str(exc) == "binding_conflict":
                    skipped_conflicts += 1
                    continue
                errors.append(str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(str(exc))

    if not errors:
        _DEFAULT_BINDINGS_ENSURED = True

    return {
        "success": len(errors) == 0,
        "ensured": len(errors) == 0,
        "created_count": created_count,
        "updated_count": updated_count,
        "unchanged_count": unchanged_count,
        "skipped_conflicts": skipped_conflicts,
        "errors": errors,
    }


def _fetch_persistent_bindings(
    *,
    event_type: str | None = None,
    enabled_only: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    manager = get_instance_manager()
    if not hasattr(manager, "list_event_bindings"):
        return []
    try:
        bindings = manager.list_event_bindings(
            event_type=event_type,
            enabled_only=enabled_only,
            limit=limit,
        )
    except Exception:
        return []

    payload: list[dict[str, Any]] = []
    for item in bindings:
        if isinstance(item, EventWorkflowBinding):
            payload.append(
                {
                    "binding_id": item.binding_id,
                    "event_type": item.event_type,
                    "workflow_id": item.workflow_id,
                    "input_mapping": dict(item.input_mapping),
                    "enabled": bool(item.enabled),
                    "created_at": item.created_at.isoformat()
                    if item.created_at
                    else None,
                    "updated_at": item.updated_at.isoformat()
                    if item.updated_at
                    else None,
                    "created_by": item.created_by,
                    "updated_by": item.updated_by,
                    "revision": int(item.revision),
                    "source": "persistent",
                }
            )
    return payload


def list_event_workflow_bindings(
    *,
    event_type: str | None = None,
    enabled_only: bool = False,
    include_env_fallback: bool = True,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return event-workflow bindings for introspection and diagnostics."""

    bindings = _fetch_persistent_bindings(
        event_type=event_type,
        enabled_only=enabled_only,
        limit=limit,
    )
    seen_pairs = {
        (str(b.get("event_type") or ""), str(b.get("workflow_id") or ""))
        for b in bindings
    }

    if include_env_fallback:
        event_keys = (
            [event_type]
            if isinstance(event_type, str) and event_type.strip()
            else list(EVENT_WORKFLOW_ID_ENV_MAP.keys())
        )
        for key in event_keys:
            if not isinstance(key, str) or not key.strip():
                continue
            workflow_id = _workflow_id_for_event(key)
            if not workflow_id:
                continue
            pair = (key, workflow_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            bindings.append(
                {
                    "binding_id": None,
                    "event_type": key,
                    "workflow_id": workflow_id,
                    "input_mapping": {},
                    "enabled": True,
                    "created_at": None,
                    "updated_at": None,
                    "created_by": None,
                    "updated_by": None,
                    "revision": None,
                    "source": "environment",
                }
            )

    def _sort_key(item: dict[str, Any]) -> tuple[str, int, str]:
        source = str(item.get("source") or "")
        source_rank = 0 if source == "persistent" else 1
        return (
            str(item.get("event_type") or ""),
            source_rank,
            str(item.get("workflow_id") or ""),
        )

    return sorted(bindings, key=_sort_key)[: max(1, min(limit, 500))]


def _resolve_bindings_for_event(event_type: str) -> list[dict[str, Any]]:
    all_bindings = list_event_workflow_bindings(
        event_type=event_type,
        enabled_only=True,
        include_env_fallback=True,
        limit=200,
    )
    return [
        binding
        for binding in all_bindings
        if str(binding.get("event_type") or "").strip() == event_type
    ]


def _extract_path_value(payload: dict[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = payload
    for segment in path.split("."):
        segment_clean = segment.strip()
        if not segment_clean:
            return False, None
        if not isinstance(current, dict) or segment_clean not in current:
            return False, None
        current = current[segment_clean]
    return True, current


def _resolve_mapping_value(
    expression: str,
    *,
    event_payload: dict[str, Any],
    base_inputs: dict[str, Any],
) -> tuple[bool, Any]:
    text = str(expression or "").strip()
    if not text:
        return False, None
    # Backward compatibility: persistent bindings may store template-style
    # expressions (e.g. "{{event.concept_id}}"). Treat these as plain mapping
    # expressions so they resolve deterministically instead of flowing through
    # as literal strings.
    if text.startswith("{{") and text.endswith("}}"):
        inner = text[2:-2].strip()
        if inner:
            text = inner
    if text.startswith("event."):
        return _extract_path_value(event_payload, text.removeprefix("event."))
    if text.startswith("inputs."):
        return _extract_path_value(base_inputs, text.removeprefix("inputs."))
    return True, text


def _apply_input_mapping(
    *,
    base_inputs: dict[str, Any],
    event_payload: dict[str, Any],
    input_mapping: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    merged_inputs = dict(base_inputs)
    unresolved: list[str] = []
    for target_key, expression in input_mapping.items():
        target_key_clean = str(target_key or "").strip()
        if not target_key_clean:
            continue
        found, value = _resolve_mapping_value(
            str(expression or ""),
            event_payload=event_payload,
            base_inputs=base_inputs,
        )
        if found:
            merged_inputs[target_key_clean] = value
        else:
            unresolved.append(target_key_clean)
    return merged_inputs, unresolved


def _launch_single_event_binding(
    *,
    event_type: str,
    event_id: str,
    user_id: str | None,
    org_id: str | None,
    namespace_override: str | None,
    workflow_id: str,
    inputs: dict[str, Any],
    event_payload: dict[str, Any],
) -> dict[str, Any]:
    safe_event_id = str(event_id or "").strip()
    resolved_workflow_id = str(workflow_id or "").strip()
    namespace = _normalise_namespace_override(namespace_override) or _build_namespace(
        user_id,
        org_id,
    )
    event_idempotency_key = _build_event_idempotency_key(
        workflow_id=resolved_workflow_id,
        event_type=event_type,
        event_id=safe_event_id,
    )
    payload_inputs = dict(inputs or {})
    existing_event = payload_inputs.get("event")
    existing_event_dict = existing_event if isinstance(existing_event, dict) else {}
    payload_inputs["event"] = {
        **existing_event_dict,
        **dict(event_payload),
        "event_type": event_type,
        "event_id": safe_event_id,
        "idempotency_key": event_idempotency_key,
    }

    manager = get_instance_manager()
    instance_id, created_new = manager.create_instance_for_event(
        resolved_workflow_id,
        user_id=(user_id or "anonymous"),
        org_id=(org_id or "default"),
        namespace=namespace,
        event_idempotency_key=event_idempotency_key,
        source_event_type=event_type,
        source_event_id=safe_event_id,
        inputs=payload_inputs,
    )

    logger.info(
        "[workflow_event] %s event=%s workflow=%s instance=%s created_new=%s",
        "triggered" if created_new else "reused",
        event_type,
        resolved_workflow_id,
        instance_id,
        created_new,
    )
    outcome = "triggered" if created_new else "reused"
    reason = "created_new_instance" if created_new else "idempotent_reuse"
    hint = (
        "Created a new durable workflow instance for this event."
        if created_new
        else "Reused an existing durable workflow instance for this event idempotency key."
    )
    return {
        "success": True,
        "triggered": created_new,
        "outcome": outcome,
        "reason": reason,
        "hint": hint,
        "workflow_id": resolved_workflow_id,
        "instance_id": instance_id,
        "event_type": event_type,
        "event_id": safe_event_id,
        "idempotency_key": event_idempotency_key,
        "idempotent_reused": not created_new,
    }


def launch_event_workflow(
    *,
    event_type: str,
    event_id: str,
    user_id: str | None,
    org_id: str | None,
    namespace: str | None = None,
    inputs: dict[str, Any] | None = None,
    workflow_id: str | None = None,
    event_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Launch configured durable workflow binding(s) for an event."""

    integration_enabled = get_event_workflow_integration_enabled(default=True)
    if not integration_enabled:
        return {
            "success": False,
            "triggered": False,
            "outcome": "not_triggered",
            "event_type": event_type,
            "reason": "integration_disabled",
            "hint": "Set VON_EVENT_WORKFLOW_INTEGRATION_ENABLE=1 to enable event-driven workflow integration.",
        }

    durable_enabled = get_durable_workflows_enabled(default=False)
    if not durable_enabled:
        return {
            "success": False,
            "triggered": False,
            "outcome": "not_triggered",
            "event_type": event_type,
            "reason": "durable_disabled",
            "hint": "Set VON_DURABLE_WORKFLOWS_ENABLE=1 to enable event-driven workflow execution.",
        }

    safe_event_id = str(event_id or "").strip()
    if not safe_event_id:
        return {
            "success": False,
            "triggered": False,
            "outcome": "not_triggered",
            "event_type": event_type,
            "reason": "missing_event_id",
            "hint": "Provide a non-empty event_id so idempotent event workflow launch can proceed.",
        }
    resolved_user, resolved_org = resolve_event_actor_context(
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
    )
    resolved_namespace = _normalise_namespace_override(namespace)

    if isinstance(workflow_id, str) and workflow_id.strip():
        resolved_bindings = [
            {
                "binding_id": None,
                "event_type": event_type,
                "workflow_id": workflow_id.strip(),
                "input_mapping": {},
                "enabled": True,
                "source": "explicit",
            }
        ]
    else:
        resolved_bindings = _resolve_bindings_for_event(event_type)

    if not resolved_bindings:
        env_name = EVENT_WORKFLOW_ID_ENV_MAP.get(event_type)
        hint = (
            "Register an event binding using workflow_bind_event, or configure "
            f"{env_name} with a workflow concept ID."
            if env_name
            else "Register an event binding using workflow_bind_event for this event type."
        )
        return {
            "success": False,
            "triggered": False,
            "outcome": "not_triggered",
            "event_type": event_type,
            "reason": "workflow_not_configured",
            "workflow_id_env": env_name,
            "hint": hint,
        }

    raw_event_payload = (
        dict(event_payload)
        if isinstance(event_payload, dict)
        else dict(inputs or {})
        if isinstance(inputs, dict)
        else {}
    )
    raw_event_payload.setdefault("event_type", event_type)
    raw_event_payload.setdefault("event_id", safe_event_id)
    if "concept_id" not in raw_event_payload:
        raw_event_payload["concept_id"] = safe_event_id

    launches: list[dict[str, Any]] = []
    for binding in resolved_bindings:
        binding_workflow_id = str(binding.get("workflow_id") or "").strip()
        if not binding_workflow_id:
            continue
        input_mapping = _normalise_event_binding_mapping(
            binding.get("input_mapping")
            if isinstance(binding.get("input_mapping"), dict)
            else {},
        )
        mapped_inputs, unresolved_targets = _apply_input_mapping(
            base_inputs=dict(inputs or {}),
            event_payload=raw_event_payload,
            input_mapping=input_mapping,
        )
        try:
            launch = _launch_single_event_binding(
                event_type=event_type,
                event_id=safe_event_id,
                user_id=resolved_user,
                org_id=resolved_org,
                namespace_override=resolved_namespace,
                workflow_id=binding_workflow_id,
                inputs=mapped_inputs,
                event_payload=raw_event_payload,
            )
            launch["binding_source"] = binding.get("source")
            launch["binding_id"] = binding.get("binding_id")
            launch["unresolved_input_mappings"] = unresolved_targets
            launches.append(launch)
        except Exception as exc:  # pragma: no cover - defensive
            launches.append(
                {
                    "success": False,
                    "triggered": False,
                    "outcome": "not_triggered",
                    "event_type": event_type,
                    "event_id": safe_event_id,
                    "workflow_id": binding_workflow_id,
                    "binding_source": binding.get("source"),
                    "binding_id": binding.get("binding_id"),
                    "error": str(exc),
                    "reason": "launch_failed",
                    "hint": "Inspect error details for this binding launch failure.",
                    "error_code": "launch_failed",
                    "unresolved_input_mappings": unresolved_targets,
                }
            )

    if not launches:
        return {
            "success": False,
            "triggered": False,
            "outcome": "not_triggered",
            "event_type": event_type,
            "event_id": safe_event_id,
            "reason": "workflow_not_configured",
            "hint": "No executable event binding resolved for this event launch request.",
        }

    if len(launches) == 1:
        result = dict(launches[0])
        if not isinstance(result.get("outcome"), str):
            if bool(result.get("triggered")):
                result["outcome"] = "triggered"
            elif bool(result.get("idempotent_reused")):
                result["outcome"] = "reused"
            else:
                result["outcome"] = "not_triggered"
        if not isinstance(result.get("reason"), str) or not result.get("reason"):
            if result["outcome"] == "triggered":
                result["reason"] = "created_new_instance"
            elif result["outcome"] == "reused":
                result["reason"] = "idempotent_reuse"
            else:
                result["reason"] = "not_triggered"
        result["launches"] = launches
        result["launch_count"] = 1
        result["triggered_count"] = 1 if bool(result.get("triggered")) else 0
        result["reused_count"] = 1 if bool(result.get("idempotent_reused")) else 0
        result["success_count"] = 1 if bool(result.get("success")) else 0
        result["failure_count"] = 0 if bool(result.get("success")) else 1
        return result

    triggered_count = sum(1 for item in launches if bool(item.get("triggered")))
    success_count = sum(1 for item in launches if bool(item.get("success")))
    reused_count = sum(1 for item in launches if bool(item.get("idempotent_reused")))
    non_trigger_reasons = sorted(
        {
            str(item.get("reason")).strip()
            for item in launches
            if isinstance(item.get("reason"), str) and str(item.get("reason")).strip()
        }
    )

    if triggered_count > 0:
        outcome = "triggered"
        reason = "at_least_one_binding_triggered"
        hint = "At least one event binding created a new workflow instance."
    elif reused_count > 0:
        outcome = "reused"
        reason = "idempotent_reuse"
        hint = "No new instance was created because an idempotent event instance already exists."
    else:
        outcome = "not_triggered"
        reason = (
            non_trigger_reasons[0]
            if len(non_trigger_reasons) == 1
            else "multiple_non_trigger_reasons"
            if non_trigger_reasons
            else "not_triggered"
        )
        hint = "Inspect launches[] for per-binding reason/error details."

    return {
        "success": success_count == len(launches),
        "triggered": triggered_count > 0,
        "outcome": outcome,
        "reason": reason,
        "hint": hint,
        "event_type": event_type,
        "event_id": safe_event_id,
        "launches": launches,
        "launch_count": len(launches),
        "triggered_count": triggered_count,
        "reused_count": reused_count,
        "success_count": success_count,
        "failure_count": len(launches) - success_count,
        "non_trigger_reasons": non_trigger_reasons,
    }


def maybe_launch_task_created_workflow(
    *,
    task_concept_id: str,
    created_by_concept_id: str | None,
    organisation_concept_id: str | None,
    namespace: str | None = None,
    title: str | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    return launch_event_workflow(
        event_type=EVENT_TYPE_TASK_CREATED,
        event_id=str(task_concept_id),
        user_id=created_by_concept_id,
        org_id=organisation_concept_id,
        namespace=namespace,
        event_payload={
            "task_concept_id": task_concept_id,
            "task_title": title,
            "task_priority": priority,
        },
        inputs={
            "task_concept_id": task_concept_id,
            "task_title": title,
            "task_priority": priority,
        },
    )


def maybe_launch_task_status_workflow(
    *,
    task_concept_id: str,
    previous_status: str | None,
    new_status: str,
    updated_at_iso: str | None,
    created_by_concept_id: str | None,
    organisation_concept_id: str | None,
    namespace: str | None = None,
) -> dict[str, Any]:
    # "Key status changes" are configurable and default to high-signal states.
    if new_status.strip().lower() not in _task_status_trigger_values():
        return {
            "success": False,
            "triggered": False,
            "outcome": "not_triggered",
            "event_type": EVENT_TYPE_TASK_STATUS_CHANGED,
            "workflow_id_env": EVENT_WORKFLOW_ID_ENV_MAP.get(
                EVENT_TYPE_TASK_STATUS_CHANGED
            ),
            "reason": "status_not_configured_for_trigger",
            "hint": "Update VON_EVENT_TASK_STATUS_TRIGGER_VALUES to include this status if a trigger is expected.",
        }

    # Include timestamp so different real transitions can still trigger while
    # replay of the same event remains idempotent.
    event_id = f"{task_concept_id}:{previous_status or 'unknown'}->{new_status}:{updated_at_iso or 'na'}"

    return launch_event_workflow(
        event_type=EVENT_TYPE_TASK_STATUS_CHANGED,
        event_id=event_id,
        user_id=created_by_concept_id,
        org_id=organisation_concept_id,
        namespace=namespace,
        event_payload={
            "task_concept_id": task_concept_id,
            "previous_status": previous_status,
            "new_status": new_status,
            "status_changed_at": updated_at_iso,
            "concept_id": task_concept_id,
        },
        inputs={
            "task_concept_id": task_concept_id,
            "previous_status": previous_status,
            "new_status": new_status,
            "status_changed_at": updated_at_iso,
        },
    )


def maybe_launch_direct_message_workflow(
    *,
    message_concept_id: str,
    sender_id: str,
    recipient_ids: list[str],
    org_id: str | None,
    namespace: str | None = None,
) -> dict[str, Any]:
    return launch_event_workflow(
        event_type=EVENT_TYPE_DIRECT_MESSAGE_CREATED,
        event_id=str(message_concept_id),
        user_id=sender_id,
        org_id=org_id,
        namespace=namespace,
        event_payload={
            "message_concept_id": message_concept_id,
            "sender_id": sender_id,
            "recipient_ids": list(recipient_ids),
            "concept_id": message_concept_id,
        },
        inputs={
            "message_concept_id": message_concept_id,
            "sender_id": sender_id,
            "recipient_ids": list(recipient_ids),
        },
    )


def maybe_launch_type_created_workflow(
    *,
    type_concept_id: str,
    created_by_concept_id: str | None,
    organisation_concept_id: str | None,
    namespace: str | None = None,
    parent_type_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Launch workflow(s) bound to type.created for new type concepts."""

    return launch_event_workflow(
        event_type=EVENT_TYPE_TYPE_CREATED,
        event_id=str(type_concept_id),
        user_id=created_by_concept_id,
        org_id=organisation_concept_id,
        namespace=namespace,
        event_payload={
            "concept_id": type_concept_id,
            "type_concept_id": type_concept_id,
            "parent_type_ids": list(parent_type_ids or []),
            "kind": "type",
        },
        inputs={
            "type_concept_id": type_concept_id,
            "parent_type_ids": list(parent_type_ids or []),
        },
    )


def maybe_launch_vontology_mutation_workflow(
    *,
    mutation_event_type: str,
    mutation_id: str,
    event_payload: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Emit both specific and catch-all Vontology mutation events.

    This keeps event semantics general for future mutation-triggered workflows
    without requiring additional engine-level event wiring.
    """

    mutation_type = str(mutation_event_type or "").strip()
    safe_mutation_id = str(mutation_id or "").strip()
    if not mutation_type:
        return {
            "success": False,
            "triggered": False,
            "reason": "missing_mutation_event_type",
        }
    if not safe_mutation_id:
        return {
            "success": False,
            "triggered": False,
            "reason": "missing_mutation_id",
            "event_type": mutation_type,
        }

    resolved_user, resolved_org = resolve_event_actor_context(
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
    )

    payload = dict(event_payload or {})
    payload.setdefault("mutation_event_type", mutation_type)
    payload.setdefault("mutation_id", safe_mutation_id)

    specific_inputs = dict(inputs or {})
    specific_inputs.setdefault("mutation_event_type", mutation_type)
    specific_inputs.setdefault("mutation_id", safe_mutation_id)

    specific_result = launch_event_workflow(
        event_type=mutation_type,
        event_id=safe_mutation_id,
        user_id=resolved_user,
        org_id=resolved_org,
        namespace=namespace,
        event_payload=payload,
        inputs=specific_inputs,
    )

    catch_all_inputs = dict(specific_inputs)
    catch_all_inputs.setdefault("event_type", mutation_type)
    catch_all_payload = dict(payload)
    catch_all_payload.setdefault("event_type", mutation_type)

    catch_all_result = launch_event_workflow(
        event_type=EVENT_TYPE_VONTOLOGY_MUTATED,
        event_id=f"{mutation_type}:{safe_mutation_id}",
        user_id=resolved_user,
        org_id=resolved_org,
        namespace=namespace,
        event_payload=catch_all_payload,
        inputs=catch_all_inputs,
    )

    return {
        "success": bool(specific_result.get("success")) and bool(
            catch_all_result.get("success")
        ),
        "triggered": bool(specific_result.get("triggered"))
        or bool(catch_all_result.get("triggered")),
        "mutation_event_type": mutation_type,
        "mutation_id": safe_mutation_id,
        "specific": specific_result,
        "catch_all": catch_all_result,
        "specific_triggered": bool(specific_result.get("triggered")),
        "catch_all_triggered": bool(catch_all_result.get("triggered")),
    }
