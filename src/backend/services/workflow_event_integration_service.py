"""Event-driven workflow launch helpers (WS6 / JVNAUTOSCI-1090).

This module provides one canonical pathway from domain events (task/message)
to durable workflow instances. Keep event-to-workflow launch logic here rather
than duplicating launch code across routes, MCP handlers, or services.
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
from ..workflows.durable.startup import get_instance_manager

logger = logging.getLogger(__name__)

# Canonical event type labels for traceability.
EVENT_TYPE_TASK_CREATED = "task.created"
EVENT_TYPE_TASK_STATUS_CHANGED = "task.status_changed"
EVENT_TYPE_DIRECT_MESSAGE_CREATED = "message.direct_created"

EVENT_WORKFLOW_ID_ENV_MAP: dict[str, str] = {
    EVENT_TYPE_TASK_CREATED: "VON_EVENT_TASK_CREATED_WORKFLOW_ID",
    EVENT_TYPE_TASK_STATUS_CHANGED: "VON_EVENT_TASK_STATUS_CHANGED_WORKFLOW_ID",
    EVENT_TYPE_DIRECT_MESSAGE_CREATED: "VON_EVENT_DIRECT_MESSAGE_WORKFLOW_ID",
}


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
    safe_org = (org_id or "default").strip() or "default"
    return f"{safe_user}/{safe_org}"


def launch_event_workflow(
    *,
    event_type: str,
    event_id: str,
    user_id: str | None,
    org_id: str | None,
    inputs: dict[str, Any] | None = None,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """Launch a configured durable workflow for an event with idempotency."""

    integration_enabled = get_event_workflow_integration_enabled(default=True)
    if not integration_enabled:
        return {
            "success": False,
            "triggered": False,
            "event_type": event_type,
            "reason": "integration_disabled",
        }

    durable_enabled = get_durable_workflows_enabled(default=False)
    if not durable_enabled:
        return {
            "success": False,
            "triggered": False,
            "event_type": event_type,
            "reason": "durable_disabled",
            "hint": "Set VON_DURABLE_WORKFLOWS_ENABLE=1 to enable event-driven workflow execution.",
        }

    resolved_workflow_id = workflow_id or _workflow_id_for_event(event_type)
    if not resolved_workflow_id:
        env_name = EVENT_WORKFLOW_ID_ENV_MAP.get(event_type)
        hint = (
            f"Configure {env_name} with a workflow concept ID (for example #V#todo_refresh_workflow)."
            if env_name
            else "Configure a workflow_id explicitly for this event type."
        )
        return {
            "success": False,
            "triggered": False,
            "event_type": event_type,
            "reason": "workflow_not_configured",
            "workflow_id_env": env_name,
            "hint": hint,
        }

    safe_event_id = str(event_id or "").strip()
    if not safe_event_id:
        return {
            "success": False,
            "triggered": False,
            "event_type": event_type,
            "workflow_id": resolved_workflow_id,
            "reason": "missing_event_id",
        }

    namespace = _build_namespace(user_id, org_id)
    event_idempotency_key = _build_event_idempotency_key(
        workflow_id=resolved_workflow_id,
        event_type=event_type,
        event_id=safe_event_id,
    )
    payload_inputs = dict(inputs or {})
    payload_inputs.setdefault(
        "event",
        {
            "event_type": event_type,
            "event_id": safe_event_id,
            "idempotency_key": event_idempotency_key,
        },
    )

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

    result = {
        "success": True,
        "triggered": created_new,
        "workflow_id": resolved_workflow_id,
        "instance_id": instance_id,
        "event_type": event_type,
        "event_id": safe_event_id,
        "idempotency_key": event_idempotency_key,
        "idempotent_reused": not created_new,
    }
    logger.info(
        "[workflow_event] %s event=%s workflow=%s instance=%s created_new=%s",
        "triggered" if created_new else "reused",
        event_type,
        resolved_workflow_id,
        instance_id,
        created_new,
    )
    return result


def maybe_launch_task_created_workflow(
    *,
    task_concept_id: str,
    created_by_concept_id: str | None,
    organisation_concept_id: str | None,
    title: str | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    return launch_event_workflow(
        event_type=EVENT_TYPE_TASK_CREATED,
        event_id=str(task_concept_id),
        user_id=created_by_concept_id,
        org_id=organisation_concept_id,
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
) -> dict[str, Any]:
    # "Key status changes" are configurable and default to high-signal states.
    if new_status.strip().lower() not in _task_status_trigger_values():
        return {
            "success": False,
            "triggered": False,
            "event_type": EVENT_TYPE_TASK_STATUS_CHANGED,
            "workflow_id_env": EVENT_WORKFLOW_ID_ENV_MAP.get(
                EVENT_TYPE_TASK_STATUS_CHANGED
            ),
            "reason": "status_not_configured_for_trigger",
        }

    # Include timestamp so different real transitions can still trigger while
    # replay of the same event remains idempotent.
    event_id = f"{task_concept_id}:{previous_status or 'unknown'}->{new_status}:{updated_at_iso or 'na'}"

    return launch_event_workflow(
        event_type=EVENT_TYPE_TASK_STATUS_CHANGED,
        event_id=event_id,
        user_id=created_by_concept_id,
        org_id=organisation_concept_id,
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
) -> dict[str, Any]:
    return launch_event_workflow(
        event_type=EVENT_TYPE_DIRECT_MESSAGE_CREATED,
        event_id=str(message_concept_id),
        user_id=sender_id,
        org_id=org_id,
        inputs={
            "message_concept_id": message_concept_id,
            "sender_id": sender_id,
            "recipient_ids": list(recipient_ids),
        },
    )

