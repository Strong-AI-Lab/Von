"""Bootstrap a managed schedule for proactive paper recommendation refreshes."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import threading
from typing import Any

from .namespace_service import coerce_namespace, derive_namespace_for_actor
from .paper_recommendation_constants import PAPER_RECOMMENDATION_WORKFLOW_ID
from ..workflows.durable.models import ScheduleType, WorkflowSchedule
from ..workflows.durable.startup import get_instance_manager

PAPER_RECOMMENDATION_BACKGROUND_SCHEDULE_MANAGED_KEY = (
    "paper_recommendation_background_v1"
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


def _desired_schedule_config() -> dict[str, Any]:
    user_id = str(
        os.getenv("VON_PAPER_RECOMMENDATION_SCHEDULE_USER_ID", "#V#system")
    ).strip() or "#V#system"
    org_id = str(
        os.getenv("VON_PAPER_RECOMMENDATION_SCHEDULE_ORG_ID", "#V#default")
    ).strip() or "#V#default"
    namespace_override = coerce_namespace(
        os.getenv("VON_PAPER_RECOMMENDATION_SCHEDULE_NAMESPACE")
    )
    namespace = (
        namespace_override
        or derive_namespace_for_actor(user_id, org_id)
        or "#V#system"
    )
    interval_seconds = _coerce_int(
        os.getenv("VON_PAPER_RECOMMENDATION_SCHEDULE_INTERVAL_SECONDS"),
        default=21600,
        minimum=300,
        maximum=604800,
    )
    max_results = _coerce_int(
        os.getenv("VON_PAPER_RECOMMENDATION_SCHEDULE_MAX_RESULTS"),
        default=5,
        minimum=1,
        maximum=20,
    )
    candidate_limit = _coerce_int(
        os.getenv("VON_PAPER_RECOMMENDATION_SCHEDULE_CANDIDATE_LIMIT"),
        default=24,
        minimum=1,
        maximum=100,
    )
    return {
        "user_id": user_id,
        "org_id": org_id,
        "namespace": namespace,
        "interval_seconds": interval_seconds,
        "default_inputs": {
            "managed_schedule_key": PAPER_RECOMMENDATION_BACKGROUND_SCHEDULE_MANAGED_KEY,
            "trigger_source": "paper_recommendation_background_schedule",
            "discover_subjects_if_missing": True,
            "max_results": max_results,
            "candidate_limit": candidate_limit,
        },
    }


def _is_managed_schedule(schedule: WorkflowSchedule) -> bool:
    if schedule.workflow_id != PAPER_RECOMMENDATION_WORKFLOW_ID:
        return False
    inputs = schedule.default_inputs if isinstance(schedule.default_inputs, dict) else {}
    marker = str(inputs.get("managed_schedule_key") or "").strip()
    if marker == PAPER_RECOMMENDATION_BACKGROUND_SCHEDULE_MANAGED_KEY:
        return True
    return PAPER_RECOMMENDATION_BACKGROUND_SCHEDULE_MANAGED_KEY in str(
        schedule.description or ""
    )


def _matches_desired(schedule: WorkflowSchedule, desired: dict[str, Any]) -> bool:
    inputs = schedule.default_inputs if isinstance(schedule.default_inputs, dict) else {}
    desired_inputs = desired.get("default_inputs") or {}
    return (
        schedule.workflow_id == PAPER_RECOMMENDATION_WORKFLOW_ID
        and schedule.schedule_type == ScheduleType.INTERVAL
        and int(schedule.interval_seconds or 0) == int(desired["interval_seconds"])
        and str(schedule.user_id or "") == str(desired["user_id"])
        and str(schedule.org_id or "") == str(desired["org_id"])
        and str(schedule.namespace or "") == str(desired["namespace"])
        and str(inputs.get("managed_schedule_key") or "")
        == str(desired_inputs.get("managed_schedule_key") or "")
        and bool(inputs.get("discover_subjects_if_missing")) is True
        and int(inputs.get("max_results") or 0) == int(desired_inputs.get("max_results") or 0)
        and int(inputs.get("candidate_limit") or 0)
        == int(desired_inputs.get("candidate_limit") or 0)
    )


def ensure_paper_recommendation_background_schedule() -> dict[str, Any]:
    """Ensure a managed interval schedule exists for proactive recommendation delivery."""

    global _bootstrap_completed

    if not _env_flag("VON_PAPER_RECOMMENDATION_SCHEDULE_ENABLE", default=True):
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
        managed = [item for item in schedules if _is_managed_schedule(item)]
        matching = [item for item in managed if _matches_desired(item, desired)]

        created_count = 0
        updated_count = 0
        disabled_count = 0
        active_schedule_id: str | None = None

        if matching:
            primary = sorted(matching, key=lambda item: item.schedule_id)[0]
            active_schedule_id = primary.schedule_id
            if not primary.enabled and manager.set_schedule_enabled(primary.schedule_id, True):
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
                PAPER_RECOMMENDATION_WORKFLOW_ID,
                interval_seconds=int(desired["interval_seconds"]),
                user_id=str(desired["user_id"]),
                org_id=str(desired["org_id"]),
                namespace=str(desired["namespace"]),
                default_inputs=dict(desired["default_inputs"]),
                description=(
                    "Managed paper-recommendation background schedule "
                    f"({PAPER_RECOMMENDATION_BACKGROUND_SCHEDULE_MANAGED_KEY})"
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
            "managed_schedule_key": PAPER_RECOMMENDATION_BACKGROUND_SCHEDULE_MANAGED_KEY,
            "workflow_id": PAPER_RECOMMENDATION_WORKFLOW_ID,
            "interval_seconds": int(desired["interval_seconds"]),
            "namespace": str(desired["namespace"]),
        }


__all__ = [
    "PAPER_RECOMMENDATION_BACKGROUND_SCHEDULE_MANAGED_KEY",
    "ensure_paper_recommendation_background_schedule",
]
