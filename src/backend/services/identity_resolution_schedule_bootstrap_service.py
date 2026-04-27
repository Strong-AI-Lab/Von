"""Bootstrap helpers for long-running identity-resolution authority.

This module owns the Vontology-side bootstrap surfaces for the canonical
``#V#entity_identity_resolution_workflow``:

* an idempotent managed interval schedule (so duplicate-entity maintenance
  keeps running automatically), and
* an idempotent persistent :class:`EventWorkflowBinding` for
  ``identity_resolution.requested`` (so paper-ingest and any other producers
  of that event route through the same persisted authority surface, rather
  than via a hard-coded workflow ID in Python).  See JVNAUTOSCI-2150 phase 1.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import threading
from typing import Any

from .identity_resolution_workflow_request_service import (
    IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE,
)
from .namespace_service import coerce_namespace, derive_namespace_for_actor
from ..workflows.durable.models import ScheduleType, WorkflowSchedule
from ..workflows.durable.startup import get_instance_manager
from ..workflows.durable.entity_identity_resolution_workflow import (
    ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
    DEFAULT_AUTO_MERGE_THRESHOLD,
    DEFAULT_REVIEW_THRESHOLD,
    DEFAULT_SCAN_LIMIT,
    DEFAULT_MAX_PAIR_EVAL,
)

logger = logging.getLogger(__name__)

IDENTITY_RESOLUTION_SCHEDULE_MANAGED_KEY = "identity_resolution_background_v1"
IDENTITY_RESOLUTION_EVENT_BINDING_MANAGED_BY = (
    "identity_resolution_schedule_bootstrap_service"
)

_bootstrap_lock = threading.Lock()
_bootstrap_completed = False


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _desired_schedule_config() -> dict[str, Any]:
    user_id = str(
        os.getenv("VON_IDENTITY_RESOLUTION_SCHEDULE_USER_ID", "#V#system")
    ).strip() or "#V#system"
    org_id = str(
        os.getenv("VON_IDENTITY_RESOLUTION_SCHEDULE_ORG_ID", "#V#default")
    ).strip() or "#V#default"
    namespace_override = coerce_namespace(
        os.getenv("VON_IDENTITY_RESOLUTION_SCHEDULE_NAMESPACE")
    )
    namespace = (
        namespace_override
        or derive_namespace_for_actor(user_id, org_id)
        or "#V#system"
    )

    interval_seconds = _coerce_int(
        os.getenv("VON_IDENTITY_RESOLUTION_SCHEDULE_INTERVAL_SECONDS"),
        default=21600,  # 6 hours
        minimum=300,
        maximum=604800,
    )
    scan_limit = _coerce_int(
        os.getenv("VON_IDENTITY_RESOLUTION_SCAN_LIMIT"),
        default=DEFAULT_SCAN_LIMIT,
        minimum=20,
        maximum=2000,
    )
    max_pair_evaluations = _coerce_int(
        os.getenv("VON_IDENTITY_RESOLUTION_MAX_PAIR_EVALUATIONS"),
        default=DEFAULT_MAX_PAIR_EVAL,
        minimum=20,
        maximum=5000,
    )
    auto_threshold = _coerce_float(
        os.getenv("VON_IDENTITY_RESOLUTION_AUTO_MERGE_THRESHOLD"),
        default=DEFAULT_AUTO_MERGE_THRESHOLD,
        minimum=0.5,
        maximum=0.999,
    )
    review_threshold = _coerce_float(
        os.getenv("VON_IDENTITY_RESOLUTION_REVIEW_THRESHOLD"),
        default=DEFAULT_REVIEW_THRESHOLD,
        minimum=0.3,
        maximum=0.999,
    )
    if review_threshold > auto_threshold:
        review_threshold = auto_threshold

    default_inputs = {
        "managed_schedule_key": IDENTITY_RESOLUTION_SCHEDULE_MANAGED_KEY,
        "scan_limit": scan_limit,
        "max_pair_evaluations": max_pair_evaluations,
        "auto_apply_confidence_threshold": auto_threshold,
        "review_confidence_threshold": review_threshold,
    }

    return {
        "user_id": user_id,
        "org_id": org_id,
        "namespace": namespace,
        "interval_seconds": interval_seconds,
        "default_inputs": default_inputs,
    }


def _is_managed_identity_schedule(schedule: WorkflowSchedule) -> bool:
    if schedule.workflow_id != ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID:
        return False
    inputs = schedule.default_inputs if isinstance(schedule.default_inputs, dict) else {}
    marker = str(inputs.get("managed_schedule_key") or "").strip()
    if marker == IDENTITY_RESOLUTION_SCHEDULE_MANAGED_KEY:
        return True
    description = str(schedule.description or "")
    return IDENTITY_RESOLUTION_SCHEDULE_MANAGED_KEY in description


def _matches_desired(schedule: WorkflowSchedule, desired: dict[str, Any]) -> bool:
    return (
        schedule.workflow_id == ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID
        and schedule.schedule_type == ScheduleType.INTERVAL
        and int(schedule.interval_seconds or 0) == int(desired["interval_seconds"])
        and str(schedule.user_id or "") == str(desired["user_id"])
        and str(schedule.org_id or "") == str(desired["org_id"])
        and str(schedule.namespace or "") == str(desired["namespace"])
    )


def ensure_identity_resolution_background_schedule() -> dict[str, Any]:
    """Ensure the managed identity-resolution background schedule exists and is enabled."""
    global _bootstrap_completed

    if not _env_flag("VON_IDENTITY_RESOLUTION_SCHEDULE_ENABLE", default=True):
        _bootstrap_completed = True
        return {
            "success": True,
            "ensured": False,
            "reason": "schedule_bootstrap_disabled",
            "created_count": 0,
            "updated_count": 0,
            "disabled_count": 0,
        }

    with _bootstrap_lock:
        if _bootstrap_completed:
            return {
                "success": True,
                "ensured": True,
                "reason": "already_bootstrapped",
                "created_count": 0,
                "updated_count": 0,
                "disabled_count": 0,
            }

        desired = _desired_schedule_config()
        manager = get_instance_manager()
        schedules = manager.list_schedules(limit=500)
        managed = [item for item in schedules if _is_managed_identity_schedule(item)]
        matching = [item for item in managed if _matches_desired(item, desired)]

        created_count = 0
        updated_count = 0
        disabled_count = 0
        active_schedule_id: str | None = None

        if matching:
            primary = sorted(matching, key=lambda item: item.schedule_id)[0]
            active_schedule_id = primary.schedule_id
            if not primary.enabled:
                if manager.set_schedule_enabled(primary.schedule_id, True):
                    updated_count += 1
            for duplicate in matching[1:]:
                if duplicate.enabled and manager.set_schedule_enabled(
                    duplicate.schedule_id, False
                ):
                    disabled_count += 1

            for mismatch in managed:
                if mismatch.schedule_id == primary.schedule_id:
                    continue
                if mismatch.enabled and manager.set_schedule_enabled(
                    mismatch.schedule_id, False
                ):
                    disabled_count += 1
        else:
            for mismatch in managed:
                if mismatch.enabled and manager.set_schedule_enabled(
                    mismatch.schedule_id, False
                ):
                    disabled_count += 1

            schedule = WorkflowSchedule.create_interval(
                ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
                interval_seconds=int(desired["interval_seconds"]),
                user_id=str(desired["user_id"]),
                org_id=str(desired["org_id"]),
                namespace=str(desired["namespace"]),
                default_inputs=dict(desired["default_inputs"]),
                description=(
                    "Managed identity-resolution background schedule "
                    f"({IDENTITY_RESOLUTION_SCHEDULE_MANAGED_KEY})"
                ),
                start_at=datetime.now(timezone.utc),
            )
            active_schedule_id = manager.create_schedule(schedule)
            created_count += 1

        _bootstrap_completed = True
        return {
            "success": True,
            "ensured": True,
            "created_count": created_count,
            "updated_count": updated_count,
            "disabled_count": disabled_count,
            "active_schedule_id": active_schedule_id,
            "managed_schedule_key": IDENTITY_RESOLUTION_SCHEDULE_MANAGED_KEY,
            "workflow_id": ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
            "interval_seconds": int(desired["interval_seconds"]),
            "namespace": str(desired["namespace"]),
        }


def ensure_identity_resolution_event_bindings() -> dict[str, Any]:
    """Register the persistent event binding for ``identity_resolution.requested``.

    This is the canonical Vontology-side authority that connects the
    ``identity_resolution.requested`` event (emitted, for example, by paper
    ingest after scholarly-author materialisation) to the durable
    ``#V#entity_identity_resolution_workflow``.  Routing is therefore a
    persisted-binding decision, not a Python constant.

    The function is idempotent: it uses ``upsert_event_binding`` with
    ``replace_existing=True`` so repeated startup calls converge on a single
    enabled binding without producing duplicates.
    """

    if not _env_flag(
        "VON_IDENTITY_RESOLUTION_EVENT_BINDING_ENABLE", default=True
    ):
        return {
            "success": True,
            "ensured": False,
            "reason": "event_binding_bootstrap_disabled",
            "created_count": 0,
            "updated_count": 0,
            "binding_count": 0,
            "bindings": [],
        }

    manager = get_instance_manager()
    binding, created, updated = manager.upsert_event_binding(
        event_type=IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE,
        workflow_id=ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
        input_mapping={},
        enabled=True,
        actor=IDENTITY_RESOLUTION_EVENT_BINDING_MANAGED_BY,
        replace_existing=True,
    )
    return {
        "success": True,
        "ensured": True,
        "created_count": 1 if created else 0,
        "updated_count": 1 if updated else 0,
        "binding_count": 1,
        "bindings": [binding.to_status_dict()],
        "event_type": IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE,
        "workflow_id": ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
    }

