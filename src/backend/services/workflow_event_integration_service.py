"""Event-driven workflow launch helpers (WS6 / JVNAUTOSCI-1090).

This module is the canonical pathway from domain events to durable workflow
instances. Keep event-to-workflow launch logic here rather than duplicating
launch code across routes, MCP handlers, or services.
"""

from __future__ import annotations

import hashlib
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Iterator, Mapping

from ..workflows.durable.models import EventWorkflowBinding
from ..workflows.durable.startup import get_instance_manager
from ..workflows.durable.workflow_instance_submission_service import (
    submit_verified_workflow_instance,
)
from ..workflows.engine import (
    compile_transition_condition_spec,
    evaluate_transition_condition_spec,
)
from .episode_evaluation_workflow_contracts import (
    EPISODE_EVALUATION_AUTOTRIGGER_ENV,
    EPISODE_EVALUATION_AUTOTRIGGER_LEGACY_ENV,
    EPISODE_EVALUATION_DEFAULT_MAX_DEPTH,
    EPISODE_EVALUATION_MAX_DEPTH_ENV,
    EPISODE_EVALUATION_WORKFLOW_ID,
    EVENT_TYPE_TURN_COMPLETION_GATE_FINALISED,
    EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
)
from .feature_flags import (
    get_durable_workflows_enabled,
    get_event_workflow_integration_enabled,
)
from .namespace_service import (
    coerce_namespace,
    concept_id_to_namespace_slug,
    derive_actor_context_from_namespace,
    derive_namespace_for_actor,
)

logger = logging.getLogger(__name__)

# Canonical event type labels for traceability.
EVENT_TYPE_TASK_CREATED = "task.created"
EVENT_TYPE_TASK_STATUS_CHANGED = "task.status_changed"
EVENT_TYPE_EFFORT_UNIT_COMPLETED = "effort_unit.completed"
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
EVENT_TYPE_SCOPED_ASSERTION_UPSERTED = "scoped_assertion.upserted"
EVENT_TYPE_SCOPED_ASSERTION_RETRACTED = "scoped_assertion.retracted"
EVENT_TYPE_VONTOLOGY_MUTATED = "vontology.mutated"
EVENT_TYPE_FILE_COPY_UPLOADED = "file_copy.uploaded"

FILE_COPY_UPLOAD_EVENT_BINDING_ENABLE_ENV = "VON_FILE_COPY_UPLOAD_EVENT_BINDING_ENABLE"
FILE_COPY_UPLOAD_EVENT_BINDING_MANAGED_BY = (
    "workflow_event_integration:file_copy_upload"
)

_DEFAULT_BINDINGS_ENSURED = False
_EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REASONS: ContextVar[tuple[str, ...]] = ContextVar(
    "event_workflow_launch_suppression_reasons",
    default=(),
)


def current_event_workflow_launch_suppression_reason() -> str | None:
    """Return the innermost request-local event-launch suppression reason."""

    reasons = _EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REASONS.get()
    return reasons[-1] if reasons else None


def event_workflow_launches_suppressed() -> bool:
    """Return whether event-driven workflow launch is suppressed in this context."""

    return bool(_EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REASONS.get())


@contextmanager
def suppress_event_workflow_launches(reason: str) -> Iterator[None]:
    """Suppress event-workflow fan-out within one nested execution context.

    The context is copied into internal MCP handler workers by the bounded
    transport, while independent threads retain their own default. Resetting
    the token preserves an outer suppression reason when scopes are nested.
    """

    cleaned_reason = str(reason or "").strip() or "unspecified"
    current_reasons = _EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REASONS.get()
    token = _EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REASONS.set(
        (*current_reasons, cleaned_reason)
    )
    try:
        yield
    finally:
        _EVENT_WORKFLOW_LAUNCH_SUPPRESSION_REASONS.reset(token)


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _coerce_non_negative_int(value: Any, *, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def ensure_file_copy_upload_event_binding() -> dict[str, Any]:
    """Ensure the canonical persisted binding for file-copy upload events."""

    if not _env_flag(FILE_COPY_UPLOAD_EVENT_BINDING_ENABLE_ENV, default=True):
        return {
            "success": True,
            "ensured": False,
            "reason": "event_binding_bootstrap_disabled",
            "created_count": 0,
            "updated_count": 0,
            "binding_count": 0,
            "bindings": [],
        }

    from ..workflows.durable.file_copy_upload_handler_workflow import (
        FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
    )

    manager = get_instance_manager()
    binding, created, updated = manager.upsert_event_binding(
        event_type=EVENT_TYPE_FILE_COPY_UPLOADED,
        workflow_id=FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
        input_mapping={},
        enabled=True,
        actor=FILE_COPY_UPLOAD_EVENT_BINDING_MANAGED_BY,
        replace_existing=True,
    )
    return {
        "success": True,
        "ensured": True,
        "created_count": 1 if created else 0,
        "updated_count": 1 if updated else 0,
        "binding_count": 1,
        "bindings": [binding.to_status_dict()],
        "event_type": EVENT_TYPE_FILE_COPY_UPLOADED,
        "workflow_id": FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
    }


def _ensure_default_event_workflow_bindings() -> dict[str, Any]:
    global _DEFAULT_BINDINGS_ENSURED
    if _DEFAULT_BINDINGS_ENSURED:
        return {"success": True, "ensured": False, "reason": "already_ensured"}
    report = ensure_file_copy_upload_event_binding()
    _DEFAULT_BINDINGS_ENSURED = True
    return report


def episode_evaluation_autotrigger_enabled() -> bool:
    for env_name in (
        EPISODE_EVALUATION_AUTOTRIGGER_ENV,
        EPISODE_EVALUATION_AUTOTRIGGER_LEGACY_ENV,
    ):
        raw = os.getenv(env_name)
        if raw is None:
            continue
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return True


def _episode_evaluation_max_depth() -> int:
    return max(
        0,
        _coerce_non_negative_int(
            os.getenv(EPISODE_EVALUATION_MAX_DEPTH_ENV),
            default=EPISODE_EVALUATION_DEFAULT_MAX_DEPTH,
        ),
    )


def _episode_evaluation_not_triggered(
    *,
    event_type: str,
    reason: str,
    workflow_id: str | None = None,
    attempted: bool = False,
    current_depth: int | None = None,
    next_depth: int | None = None,
    max_depth: int | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "triggered": False,
        "attempted": attempted,
        "enabled": episode_evaluation_autotrigger_enabled(),
        "event_type": event_type,
        "outcome": "not_triggered",
        "reason": reason,
        "workflow_id": workflow_id or EPISODE_EVALUATION_WORKFLOW_ID,
    }
    if current_depth is not None:
        payload["current_depth"] = current_depth
    if next_depth is not None:
        payload["episode_evaluation_depth"] = next_depth
    if max_depth is not None:
        payload["max_depth"] = max_depth
    if event_id:
        payload["event_id"] = event_id
    return payload


def _derive_episode_evaluation_event_id(
    *,
    request_id: str | None,
    session_id: str | None,
    workflow_id: str | None,
    incident_text: str | None,
) -> str:
    if request_id:
        return request_id
    identity_seed = "|".join(
        part
        for part in (session_id, workflow_id, incident_text)
        if isinstance(part, str) and part.strip()
    )
    identity_digest = hashlib.sha256(identity_seed.encode("utf-8")).hexdigest()[:16]
    return f"{session_id or 'sessionless'}:{identity_digest}"


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
    safe_user = concept_id_to_namespace_slug(user_id) or "anonymous"
    safe_org = concept_id_to_namespace_slug(org_id)

    namespace = derive_namespace_for_actor(safe_user, safe_org)
    if namespace:
        return namespace

    # Keep the fallback canonical even if upstream identifiers are malformed.
    if safe_org:
        return f"#V#{safe_user}@{safe_org}"
    return f"#V#{safe_user}"


def _normalise_namespace_override(namespace: str | None) -> str | None:
    return coerce_namespace(namespace)


def _derive_actor_context_from_namespace(
    namespace: str | None,
) -> tuple[str | None, str | None]:
    """Infer user/org hints from a namespace string when available."""

    namespace_clean = _normalise_namespace_override(namespace)
    if namespace_clean is None:
        return None, None
    return derive_actor_context_from_namespace(namespace_clean)


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
    resolved_org = (
        org_id.strip() if isinstance(org_id, str) and org_id.strip() else None
    )
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
            from flask import has_request_context
            from flask import session as flask_session

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
                    "condition": dict(item.condition)
                    if isinstance(item.condition, dict)
                    else None,
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


def build_event_workflow_binding_diagnostics(
    bindings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Summarise event-binding health for operator-facing workflow tooling.

    The diagnostics are intentionally conservative:
    - flag multiple enabled bindings for one event as a conflict,
    - surface disabled historical bindings so obsolete routes remain visible, and
    - flag events whose persistent bindings are all disabled.
    """

    grouped: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        event_type = str(binding.get("event_type") or "").strip()
        if not event_type:
            continue
        group = grouped.setdefault(
            event_type,
            {
                "binding_ids": [],
                "enabled_binding_ids": [],
                "disabled_binding_ids": [],
                "workflow_ids": [],
                "persistent_count": 0,
            },
        )
        binding_id = str(binding.get("binding_id") or "").strip()
        if binding_id:
            group["binding_ids"].append(binding_id)
        workflow_id = str(binding.get("workflow_id") or "").strip()
        if workflow_id and workflow_id not in group["workflow_ids"]:
            group["workflow_ids"].append(workflow_id)
        source = str(binding.get("source") or "").strip().lower()
        enabled = bool(binding.get("enabled"))
        if source == "persistent":
            group["persistent_count"] += 1
            if enabled:
                if binding_id:
                    group["enabled_binding_ids"].append(binding_id)
            else:
                if binding_id:
                    group["disabled_binding_ids"].append(binding_id)

    diagnostics: list[dict[str, Any]] = []
    for event_type in sorted(grouped):
        group = grouped[event_type]
        enabled_count = len(group["enabled_binding_ids"])
        disabled_count = len(group["disabled_binding_ids"])
        persistent_count = int(group["persistent_count"])
        severity = ""
        reason_code = ""
        hint = ""
        if enabled_count > 1:
            severity = "warning"
            reason_code = "multiple_enabled_bindings"
            hint = (
                "More than one persistent binding is enabled for this event. "
                "Disable or delete obsolete bindings so routing remains authoritative."
            )
        elif persistent_count > 0 and enabled_count == 0:
            severity = "warning"
            reason_code = "all_persistent_bindings_disabled"
            hint = (
                "Persistent bindings exist but none are enabled. Enable the canonical "
                "binding or create a new authoritative binding before relying on this event."
            )
        elif disabled_count > 0:
            severity = "info"
            reason_code = "historical_bindings_present"
            hint = (
                "Disabled historical bindings remain for this event. Delete them when "
                "they are no longer needed for audit or rollback."
            )
        if not reason_code:
            continue
        diagnostics.append(
            {
                "event_type": event_type,
                "severity": severity,
                "reason_code": reason_code,
                "binding_count": len(group["binding_ids"]),
                "persistent_count": persistent_count,
                "enabled_binding_count": enabled_count,
                "disabled_binding_count": disabled_count,
                "binding_ids": list(group["binding_ids"]),
                "enabled_binding_ids": list(group["enabled_binding_ids"]),
                "disabled_binding_ids": list(group["disabled_binding_ids"]),
                "workflow_ids": list(group["workflow_ids"]),
                "hint": hint,
            }
        )
    return diagnostics


def list_event_workflow_bindings(
    *,
    event_type: str | None = None,
    enabled_only: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return persisted event-workflow bindings for introspection and diagnostics."""

    bindings = _fetch_persistent_bindings(
        event_type=event_type,
        enabled_only=enabled_only,
        limit=limit,
    )

    def _sort_key(item: dict[str, Any]) -> tuple[str, str]:
        return (
            str(item.get("event_type") or ""),
            str(item.get("workflow_id") or ""),
        )

    return sorted(bindings, key=_sort_key)[: max(1, min(limit, 500))]


def _resolve_bindings_for_event(event_type: str) -> list[dict[str, Any]]:
    if event_type == EVENT_TYPE_FILE_COPY_UPLOADED:
        _ensure_default_event_workflow_bindings()
    all_bindings = list_event_workflow_bindings(
        event_type=event_type,
        enabled_only=True,
        limit=200,
    )
    return [
        binding
        for binding in all_bindings
        if str(binding.get("event_type") or "").strip() == event_type
    ]


def _normalise_event_binding_condition(
    condition: Any,
) -> dict[str, Any] | None:
    if condition is None:
        return None
    if not isinstance(condition, Mapping):
        raise ValueError("event_binding_condition_invalid:condition_not_mapping")
    return compile_transition_condition_spec(condition)


def _build_event_binding_condition_context(
    *,
    event_payload: dict[str, Any],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    context = dict(event_payload)
    context.setdefault("event", event_payload)
    context.setdefault("inputs", inputs)
    return context


def _evaluate_event_binding_condition(
    *,
    binding: dict[str, Any],
    event_payload: dict[str, Any],
    inputs: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None, str | None]:
    raw_condition = binding.get("condition")
    if raw_condition is None:
        return True, None, None
    try:
        condition = _normalise_event_binding_condition(raw_condition)
        if condition is None:
            return True, None, None
        matched = evaluate_transition_condition_spec(
            context=_build_event_binding_condition_context(
                event_payload=event_payload,
                inputs=inputs,
            ),
            condition_spec=condition,
        )
    except ValueError as exc:
        return False, None, str(exc)
    return bool(matched), condition, None


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


def _coerce_datetime_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        normalised = cleaned.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalised)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _extract_instance_field(instance: Any, field_name: str) -> Any:
    if isinstance(instance, dict):
        return instance.get(field_name)
    return getattr(instance, field_name, None)


def _extract_instance_id(instance: Any) -> str | None:
    raw_id = _extract_instance_field(instance, "instance_id")
    if isinstance(raw_id, str) and raw_id.strip():
        return raw_id.strip()
    return None


def _extract_instance_created_at(instance: Any) -> datetime | None:
    return _coerce_datetime_utc(_extract_instance_field(instance, "created_at"))


def _find_existing_instance_for_event(
    *,
    manager: Any,
    workflow_id: str,
    event_type: str,
    event_id: str,
) -> str | None:
    if hasattr(manager, "get_event_instance"):
        try:
            existing = manager.get_event_instance(
                workflow_id=workflow_id,
                source_event_type=event_type,
                source_event_id=event_id,
            )
        except Exception:
            existing = None
        instance_id = _extract_instance_id(existing)
        if instance_id:
            return instance_id

    if not hasattr(manager, "list_instances"):
        return None
    try:
        existing_instances = manager.list_instances(
            workflow_id=workflow_id,
            source_event_type=event_type,
            source_event_id=event_id,
            limit=1,
        )
    except Exception:
        return None
    if not isinstance(existing_instances, list) or not existing_instances:
        return None
    return _extract_instance_id(existing_instances[0])


def _workflow_background_launch_policy_for_id(
    workflow_id: str,
) -> tuple[dict[str, Any] | None, str]:
    try:
        from ..workflows.vontology_loader import (
            resolve_workflow_background_launch_policy,
        )

        return resolve_workflow_background_launch_policy(workflow_id)
    except Exception:
        return None, "policy_resolution_error"


def _workflow_policy_applies_to_event_source(policy: dict[str, Any]) -> bool:
    applies = policy.get("applies_to_sources")
    if not isinstance(applies, list):
        return True
    tokens = {
        str(item).strip().lower()
        for item in applies
        if isinstance(item, str) and item.strip()
    }
    if not tokens:
        return True
    return "all" in tokens or "event" in tokens


def _evaluate_workflow_launch_cadence(
    *,
    manager: Any,
    workflow_id: str,
) -> dict[str, Any]:
    policy, policy_source = _workflow_background_launch_policy_for_id(workflow_id)
    if not isinstance(policy, dict):
        return {
            "allowed": True,
            "reason": "cadence_policy_not_configured",
            "policy_source": policy_source,
            "policy": None,
        }

    enabled = bool(policy.get("enabled"))
    min_interval_raw = policy.get("min_interval_seconds")
    min_interval_seconds: int | None = None
    if isinstance(min_interval_raw, int):
        min_interval_seconds = min_interval_raw
    elif isinstance(min_interval_raw, float):
        min_interval_seconds = int(min_interval_raw)
    elif isinstance(min_interval_raw, str):
        try:
            min_interval_seconds = int(float(min_interval_raw.strip()))
        except ValueError:
            min_interval_seconds = None

    if (not enabled) or min_interval_seconds is None or min_interval_seconds <= 0:
        return {
            "allowed": True,
            "reason": "cadence_policy_disabled",
            "policy_source": policy_source,
            "policy": policy,
        }

    if not _workflow_policy_applies_to_event_source(policy):
        return {
            "allowed": True,
            "reason": "cadence_policy_not_applicable",
            "policy_source": policy_source,
            "policy": policy,
        }

    latest_instance: Any = None
    if hasattr(manager, "get_latest_instance_for_workflow"):
        try:
            latest_instance = manager.get_latest_instance_for_workflow(workflow_id)
        except Exception:
            latest_instance = None
    if latest_instance is None and hasattr(manager, "list_instances"):
        try:
            latest_candidates = manager.list_instances(workflow_id=workflow_id, limit=1)
        except Exception:
            latest_candidates = None
        if isinstance(latest_candidates, list) and latest_candidates:
            latest_instance = latest_candidates[0]

    latest_created_at = _extract_instance_created_at(latest_instance)
    if latest_created_at is None:
        return {
            "allowed": True,
            "reason": "cadence_window_clear",
            "policy_source": policy_source,
            "policy": policy,
        }

    next_allowed_at = latest_created_at + timedelta(seconds=min_interval_seconds)
    now_utc = datetime.now(timezone.utc)
    if now_utc >= next_allowed_at:
        return {
            "allowed": True,
            "reason": "cadence_window_clear",
            "policy_source": policy_source,
            "policy": policy,
            "latest_instance_id": _extract_instance_id(latest_instance),
            "latest_instance_created_at": latest_created_at.isoformat(),
            "next_allowed_at": next_allowed_at.isoformat(),
        }

    retry_after_seconds = int((next_allowed_at - now_utc).total_seconds())
    if retry_after_seconds < 1:
        retry_after_seconds = 1
    return {
        "allowed": False,
        "reason": "workflow_cadence_limited",
        "hint": (
            "Workflow launch skipped because the configured background cadence "
            "window is still active."
        ),
        "policy_source": policy_source,
        "policy": policy,
        "latest_instance_id": _extract_instance_id(latest_instance),
        "latest_instance_created_at": latest_created_at.isoformat(),
        "next_allowed_at": next_allowed_at.isoformat(),
        "retry_after_seconds": retry_after_seconds,
    }


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
    launch_checks_started = perf_counter()
    launch_check_timings_ms: dict[str, float] = {}
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
    idempotency_lookup_started = perf_counter()
    existing_instance_id = _find_existing_instance_for_event(
        manager=manager,
        workflow_id=resolved_workflow_id,
        event_type=event_type,
        event_id=safe_event_id,
    )
    launch_check_timings_ms["idempotency_lookup_ms"] = round(
        (perf_counter() - idempotency_lookup_started) * 1000.0,
        3,
    )
    if isinstance(existing_instance_id, str) and existing_instance_id:
        logger.info(
            "[workflow_event] reused event=%s workflow=%s instance=%s",
            event_type,
            resolved_workflow_id,
            existing_instance_id,
        )
        return {
            "success": True,
            "triggered": False,
            "outcome": "reused",
            "reason": "idempotent_reuse",
            "hint": "Reused an existing durable workflow instance for this event idempotency key.",
            "workflow_id": resolved_workflow_id,
            "instance_id": existing_instance_id,
            "event_type": event_type,
            "event_id": safe_event_id,
            "idempotency_key": event_idempotency_key,
            "idempotent_reused": True,
            "launch_check_timings_ms": {
                **launch_check_timings_ms,
                "total_ms": round((perf_counter() - launch_checks_started) * 1000.0, 3),
            },
        }

    cadence_check_started = perf_counter()
    cadence_gate = _evaluate_workflow_launch_cadence(
        manager=manager,
        workflow_id=resolved_workflow_id,
    )
    launch_check_timings_ms["cadence_check_ms"] = round(
        (perf_counter() - cadence_check_started) * 1000.0,
        3,
    )
    if not bool(cadence_gate.get("allowed")):
        return {
            "success": True,
            "triggered": False,
            "outcome": "not_triggered",
            "reason": str(cadence_gate.get("reason") or "workflow_cadence_limited"),
            "hint": str(
                cadence_gate.get("hint")
                or "Workflow launch skipped because cadence policy blocked this trigger."
            ),
            "workflow_id": resolved_workflow_id,
            "instance_id": None,
            "event_type": event_type,
            "event_id": safe_event_id,
            "idempotency_key": event_idempotency_key,
            "idempotent_reused": False,
            "cadence_policy": cadence_gate.get("policy"),
            "cadence_policy_source": cadence_gate.get("policy_source"),
            "retry_after_seconds": cadence_gate.get("retry_after_seconds"),
            "next_allowed_at": cadence_gate.get("next_allowed_at"),
            "latest_instance_id": cadence_gate.get("latest_instance_id"),
            "latest_instance_created_at": cadence_gate.get(
                "latest_instance_created_at"
            ),
            "launch_check_timings_ms": {
                **launch_check_timings_ms,
                "total_ms": round((perf_counter() - launch_checks_started) * 1000.0, 3),
            },
        }

    instance_create_started = perf_counter()
    submission = submit_verified_workflow_instance(
        manager=manager,
        workflow_id=resolved_workflow_id,
        user_id=(user_id or "anonymous"),
        org_id=(org_id or "default"),
        namespace=namespace,
        event_idempotency_key=event_idempotency_key,
        source_event_type=event_type,
        source_event_id=safe_event_id,
        inputs=payload_inputs,
    )
    launch_check_timings_ms["instance_create_ms"] = round(
        (perf_counter() - instance_create_started) * 1000.0,
        3,
    )
    if not submission.success:
        return {
            "success": False,
            "triggered": False,
            "outcome": "not_triggered",
            "reason": str(submission.error_code or "workflow_submission_failed"),
            "hint": str(
                submission.error
                or "Verified workflow submission rejected this event launch."
            ),
            "workflow_id": resolved_workflow_id,
            "instance_id": submission.instance_id,
            "event_type": event_type,
            "event_id": safe_event_id,
            "idempotency_key": event_idempotency_key,
            "idempotent_reused": False,
            "verification": dict(submission.verification),
            "submission_status": submission.status,
            "launch_check_timings_ms": {
                **launch_check_timings_ms,
                "total_ms": round((perf_counter() - launch_checks_started) * 1000.0, 3),
            },
        }

    instance_id = submission.instance_id
    created_new = submission.created_new is not False

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
        "verification": dict(submission.verification),
        "submission_status": submission.status,
        "cadence_policy": cadence_gate.get("policy"),
        "cadence_policy_source": cadence_gate.get("policy_source"),
        "launch_check_timings_ms": {
            **launch_check_timings_ms,
            "total_ms": round((perf_counter() - launch_checks_started) * 1000.0, 3),
        },
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

    suppression_reason = current_event_workflow_launch_suppression_reason()
    if suppression_reason is not None:
        return {
            "success": True,
            "triggered": False,
            "outcome": "suppressed",
            "event_type": event_type,
            "event_id": str(event_id or "").strip() or None,
            "reason": "event_workflow_launch_suppressed",
            "suppression_reason": suppression_reason,
            "event_workflow_launch_suppressed": True,
        }

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
        return {
            "success": False,
            "triggered": False,
            "outcome": "not_triggered",
            "event_type": event_type,
            "reason": "workflow_not_configured",
            "hint": "Register a persisted event binding using workflow_bind_event for this event type.",
            "launch_strategy": "resolved_persistent_bindings",
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
        condition_matched, condition, condition_error = (
            _evaluate_event_binding_condition(
                binding=binding,
                event_payload=raw_event_payload,
                inputs=dict(inputs or {}),
            )
        )
        if condition_error is not None:
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
                    "binding_condition_result": "invalid",
                    "binding_condition_error": condition_error,
                    "reason": "binding_condition_invalid",
                    "hint": "Repair the represented event binding condition metadata.",
                    "error_code": "binding_condition_invalid",
                    "unresolved_input_mappings": [],
                }
            )
            continue
        if not condition_matched:
            launches.append(
                {
                    "success": True,
                    "triggered": False,
                    "outcome": "not_triggered",
                    "event_type": event_type,
                    "event_id": safe_event_id,
                    "workflow_id": binding_workflow_id,
                    "binding_source": binding.get("source"),
                    "binding_id": binding.get("binding_id"),
                    "binding_condition": condition,
                    "binding_condition_result": False,
                    "reason": "binding_condition_not_matched",
                    "hint": "Represented event binding condition rejected this event payload.",
                    "unresolved_input_mappings": [],
                }
            )
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
            launch["binding_condition"] = condition
            launch["binding_condition_result"] = True if condition else None
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
        workflow_id = str(result.get("workflow_id") or "").strip()
        if workflow_id:
            result["selected_workflow_id"] = workflow_id
        result["launch_strategy"] = "resolved_persistent_bindings"
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
        "launch_strategy": "resolved_persistent_bindings",
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


def maybe_launch_effort_unit_completed_workflow(
    *,
    effort_unit_concept_id: str,
    effort_unit_type_ids: list[str],
    successor_effort_unit_type_ids: list[str],
    completed_at_iso: str | None,
    created_by_concept_id: str | None,
    organisation_concept_id: str | None,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Emit an effort_unit.completed event for lifecycle workflows."""

    event_id = f"{effort_unit_concept_id}:completed:{completed_at_iso or 'na'}"
    return launch_event_workflow(
        event_type=EVENT_TYPE_EFFORT_UNIT_COMPLETED,
        event_id=event_id,
        user_id=created_by_concept_id,
        org_id=organisation_concept_id,
        namespace=namespace,
        event_payload={
            "effort_unit_concept_id": effort_unit_concept_id,
            "effort_unit_type_ids": list(effort_unit_type_ids),
            "successor_effort_unit_type_ids": list(successor_effort_unit_type_ids),
            "completed_at": completed_at_iso,
            "completion_status": "completed",
            "concept_id": effort_unit_concept_id,
        },
        inputs={
            "effort_unit_concept_id": effort_unit_concept_id,
            "effort_unit_type_ids": list(effort_unit_type_ids),
            "successor_effort_unit_type_ids": list(successor_effort_unit_type_ids),
            "completed_at": completed_at_iso,
            "completion_status": "completed",
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
        "success": bool(specific_result.get("success"))
        and bool(catch_all_result.get("success")),
        "triggered": bool(specific_result.get("triggered"))
        or bool(catch_all_result.get("triggered")),
        "mutation_event_type": mutation_type,
        "mutation_id": safe_mutation_id,
        "specific": specific_result,
        "catch_all": catch_all_result,
        "specific_triggered": bool(specific_result.get("triggered")),
        "catch_all_triggered": bool(catch_all_result.get("triggered")),
    }


def maybe_launch_episode_evaluation_for_turn_completion_gate(
    *,
    request_id: str | None,
    session_id: str | None,
    namespace: str | None,
    user_id: str | None,
    org_id: str | None,
    selected_workflow_id: str | None,
    incident_text: str | None,
    maintenance_apply_repairs_default: bool,
    current_depth: int = 0,
) -> dict[str, Any]:
    enabled = episode_evaluation_autotrigger_enabled()
    event_id = _derive_episode_evaluation_event_id(
        request_id=_clean_text(request_id),
        session_id=_clean_text(session_id),
        workflow_id=_clean_text(selected_workflow_id),
        incident_text=_clean_text(incident_text),
    )
    max_depth = _episode_evaluation_max_depth()
    next_depth = _coerce_non_negative_int(current_depth) + 1
    if not enabled:
        return _episode_evaluation_not_triggered(
            event_type=EVENT_TYPE_TURN_COMPLETION_GATE_FINALISED,
            event_id=event_id,
            reason="autotrigger_disabled",
            current_depth=current_depth,
            next_depth=next_depth,
            max_depth=max_depth,
        )
    if next_depth > max_depth:
        return _episode_evaluation_not_triggered(
            event_type=EVENT_TYPE_TURN_COMPLETION_GATE_FINALISED,
            event_id=event_id,
            reason="max_depth_reached",
            attempted=False,
            current_depth=current_depth,
            next_depth=next_depth,
            max_depth=max_depth,
        )

    inputs = {
        "request_id": _clean_text(request_id),
        "session_id": _clean_text(session_id),
        "selected_workflow_id": _clean_text(selected_workflow_id),
        "incident_text": _clean_text(incident_text),
        "maintenance_apply_repairs_default": bool(maintenance_apply_repairs_default),
        "episode_evaluation_depth": next_depth,
    }
    if _clean_text(namespace):
        inputs["namespace"] = _clean_text(namespace)
    event_payload = {
        **inputs,
        "subject_kind": "turn_execution_request",
    }
    result = launch_event_workflow(
        event_type=EVENT_TYPE_TURN_COMPLETION_GATE_FINALISED,
        event_id=event_id,
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
        inputs=inputs,
        event_payload=event_payload,
    )
    return {
        **result,
        "attempted": True,
        "enabled": True,
        "current_depth": current_depth,
        "episode_evaluation_depth": next_depth,
        "max_depth": max_depth,
    }


def maybe_launch_episode_evaluation_for_workflow_terminal(
    *,
    instance: Any,
    terminal_status: str,
    final_state: str | None,
    termination_code: str | None,
    termination_detail: str | None,
) -> dict[str, Any]:
    instance_id = _extract_instance_id(instance)
    workflow_id = _clean_text(_extract_instance_field(instance, "workflow_id"))
    if not instance_id or not workflow_id:
        return _episode_evaluation_not_triggered(
            event_type=EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
            event_id=instance_id,
            reason="missing_instance_identity",
            attempted=False,
            workflow_id=workflow_id,
        )

    inputs_payload = _extract_instance_field(instance, "inputs")
    inputs_mapping = inputs_payload if isinstance(inputs_payload, dict) else {}
    current_depth = _coerce_non_negative_int(
        inputs_mapping.get("episode_evaluation_depth"),
        default=0,
    )
    max_depth = _episode_evaluation_max_depth()
    next_depth = current_depth + 1
    enabled = episode_evaluation_autotrigger_enabled()
    if not enabled:
        return _episode_evaluation_not_triggered(
            event_type=EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
            event_id=instance_id,
            reason="autotrigger_disabled",
            workflow_id=workflow_id,
            current_depth=current_depth,
            next_depth=next_depth,
            max_depth=max_depth,
        )
    if next_depth > max_depth:
        return _episode_evaluation_not_triggered(
            event_type=EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
            event_id=instance_id,
            reason="max_depth_reached",
            workflow_id=workflow_id,
            current_depth=current_depth,
            next_depth=next_depth,
            max_depth=max_depth,
        )

    namespace = _clean_text(_extract_instance_field(instance, "namespace"))
    user_id = _clean_text(_extract_instance_field(instance, "user_id"))
    org_id = _clean_text(_extract_instance_field(instance, "org_id"))
    request_id = _clean_text(inputs_mapping.get("turn_id")) or _clean_text(
        inputs_mapping.get("request_id")
    )
    session_id = _clean_text(
        inputs_mapping.get("conversation_session_id")
    ) or _clean_text(inputs_mapping.get("session_id"))
    launch_inputs = {
        "instance_id": instance_id,
        "request_id": request_id,
        "session_id": session_id,
        "selected_workflow_id": workflow_id,
        "source_terminal_status": _clean_text(terminal_status) or "completed",
        "source_final_state": _clean_text(final_state),
        "source_termination_code": _clean_text(termination_code),
        "source_termination_detail": _clean_text(termination_detail),
        "episode_evaluation_depth": next_depth,
    }
    if namespace:
        launch_inputs["namespace"] = namespace
    event_payload = {
        **launch_inputs,
        "workflow_id": workflow_id,
        "execution_trace_id": _clean_text(
            _extract_instance_field(instance, "execution_trace_id")
        ),
        "subject_kind": "workflow_terminal_instance",
    }
    result = launch_event_workflow(
        event_type=EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
        event_id=instance_id,
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
        inputs=launch_inputs,
        event_payload=event_payload,
    )
    return {
        **result,
        "attempted": True,
        "enabled": True,
        "current_depth": current_depth,
        "episode_evaluation_depth": next_depth,
        "max_depth": max_depth,
    }


def maybe_launch_file_copy_uploaded_workflow(
    *,
    file_copy_concept_id: str,
    uploaded_by_concept_id: str | None,
    organisation_concept_id: str | None,
    namespace: str | None = None,
    content_type: str | None = None,
    original_filename: str | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
    blob_uri: str | None = None,
    uploaded_at_iso: str | None = None,
    index_in_rag: bool = True,
) -> dict[str, Any]:
    """Launch upload-handler workflow for a newly uploaded file copy.

    File uploads are routed through a single selected workflow ID to avoid
    duplicate uncontrolled launches when multiple bindings may exist.
    """

    concept_id = str(file_copy_concept_id or "").strip()
    if not concept_id:
        return {
            "success": False,
            "triggered": False,
            "outcome": "not_triggered",
            "event_type": EVENT_TYPE_FILE_COPY_UPLOADED,
            "reason": "missing_file_copy_concept_id",
            "hint": "Provide file_copy_concept_id for upload-event workflow launch.",
        }

    return launch_event_workflow(
        event_type=EVENT_TYPE_FILE_COPY_UPLOADED,
        event_id=concept_id,
        user_id=uploaded_by_concept_id,
        org_id=organisation_concept_id,
        namespace=namespace,
        event_payload={
            "concept_id": concept_id,
            "file_copy_concept_id": concept_id,
            "content_type": content_type,
            "original_filename": original_filename,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "blob_uri": blob_uri,
            "uploaded_at": uploaded_at_iso,
            "index_in_rag": bool(index_in_rag),
        },
        inputs={
            "concept_id": concept_id,
            "file_copy_concept_id": concept_id,
            "content_type": content_type,
            "original_filename": original_filename,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "blob_uri": blob_uri,
            "uploaded_at": uploaded_at_iso,
            "index_in_rag": bool(index_in_rag),
        },
    )
