"""Bootstrap managed schedules for turn-pipeline monitoring workflows."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import threading
from typing import Any

from .namespace_service import coerce_namespace, derive_namespace_for_actor
from .turn_pipeline_monitoring_workflow_contracts import (
    TURN_PIPELINE_MONITORING_WORKFLOW_ID,
    TURN_PIPELINE_TIER1_REGRESSION_WORKFLOW_ID,
)
from ..workflows.durable.models import ScheduleType, WorkflowSchedule
from ..workflows.durable.startup import get_instance_manager

TURN_PIPELINE_MONITORING_SCHEDULE_MANAGED_KEY = (
    "turn_pipeline_monitoring_background_v1"
)
TURN_PIPELINE_TIER1_REGRESSION_SCHEDULE_MANAGED_KEY = (
    "turn_pipeline_tier1_regression_v1"
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


def _coerce_float(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _resolve_schedule_identity() -> dict[str, str]:
    user_id = str(
        os.getenv("VON_TURN_PIPELINE_MONITORING_SCHEDULE_USER_ID", "#V#system")
    ).strip() or "#V#system"
    org_id = str(
        os.getenv("VON_TURN_PIPELINE_MONITORING_SCHEDULE_ORG_ID", "#V#default")
    ).strip() or "#V#default"
    namespace_override = coerce_namespace(
        os.getenv("VON_TURN_PIPELINE_MONITORING_SCHEDULE_NAMESPACE")
    )
    namespace = (
        namespace_override
        or derive_namespace_for_actor(user_id, org_id)
        or "#V#system"
    )
    return {
        "user_id": user_id,
        "org_id": org_id,
        "namespace": namespace,
    }


def _desired_schedule_configs() -> dict[str, dict[str, Any]]:
    identity = _resolve_schedule_identity()
    monitoring_interval_seconds = _coerce_int(
        os.getenv("VON_TURN_PIPELINE_MONITORING_INTERVAL_SECONDS"),
        default=3600,
        minimum=300,
        maximum=604800,
    )
    regression_interval_seconds = _coerce_int(
        os.getenv("VON_TURN_PIPELINE_TIER1_REGRESSION_INTERVAL_SECONDS"),
        default=14400,
        minimum=300,
        maximum=604800,
    )
    limit = _coerce_int(
        os.getenv("VON_TURN_PIPELINE_MONITORING_LIMIT"),
        default=50,
        minimum=5,
        maximum=500,
    )
    max_cases = _coerce_int(
        os.getenv("VON_TURN_PIPELINE_MONITORING_MAX_CASES"),
        default=5,
        minimum=1,
        maximum=100,
    )
    selector_max_cases = _coerce_int(
        os.getenv("VON_TURN_PIPELINE_MONITORING_SELECTOR_MAX_CASES"),
        default=25,
        minimum=1,
        maximum=200,
    )
    regression_case_set = (
        str(os.getenv("VON_TURN_PIPELINE_TIER1_CASE_SET", "phase1_seed")).strip()
        or "phase1_seed"
    )
    regression_case_limit = _coerce_int(
        os.getenv("VON_TURN_PIPELINE_TIER1_MAX_CASES"),
        default=25,
        minimum=1,
        maximum=200,
    )
    baseline_false_success_rate_pct = _coerce_float(
        os.getenv("VON_TURN_PIPELINE_BASELINE_FALSE_SUCCESS_RATE_PCT"),
        default=0.0,
        minimum=0.0,
        maximum=100.0,
    )
    baseline_selector_accuracy_pct = _coerce_float(
        os.getenv("VON_TURN_PIPELINE_BASELINE_SELECTOR_ACCURACY_PCT"),
        default=90.0,
        minimum=0.0,
        maximum=100.0,
    )
    regression_tolerance_pct = _coerce_float(
        os.getenv("VON_TURN_PIPELINE_REGRESSION_TOLERANCE_PCT"),
        default=1.0,
        minimum=0.0,
        maximum=100.0,
    )
    latency_regression_tolerance_pct = _coerce_float(
        os.getenv("VON_TURN_PIPELINE_LATENCY_REGRESSION_TOLERANCE_PCT"),
        default=10.0,
        minimum=0.0,
        maximum=500.0,
    )

    return {
        "monitoring": {
            **identity,
            "schedule_name": "monitoring",
            "managed_schedule_key": TURN_PIPELINE_MONITORING_SCHEDULE_MANAGED_KEY,
            "workflow_id": TURN_PIPELINE_MONITORING_WORKFLOW_ID,
            "interval_seconds": monitoring_interval_seconds,
            "description": (
                "Managed turn-pipeline monitoring schedule "
                f"({TURN_PIPELINE_MONITORING_SCHEDULE_MANAGED_KEY})"
            ),
            "default_inputs": {
                "managed_schedule_key": TURN_PIPELINE_MONITORING_SCHEDULE_MANAGED_KEY,
                "namespace": identity["namespace"],
                "limit": limit,
                "max_cases": max_cases,
                "selector_case_set": regression_case_set,
                "selector_max_cases": selector_max_cases,
                "baseline_false_success_rate_pct": baseline_false_success_rate_pct,
                "baseline_selector_accuracy_pct": baseline_selector_accuracy_pct,
                "regression_tolerance_pct": regression_tolerance_pct,
                "latency_regression_tolerance_pct": latency_regression_tolerance_pct,
            },
        },
        "tier1_regression": {
            **identity,
            "schedule_name": "tier1_regression",
            "managed_schedule_key": TURN_PIPELINE_TIER1_REGRESSION_SCHEDULE_MANAGED_KEY,
            "workflow_id": TURN_PIPELINE_TIER1_REGRESSION_WORKFLOW_ID,
            "interval_seconds": regression_interval_seconds,
            "description": (
                "Managed turn-pipeline tier-1 regression schedule "
                f"({TURN_PIPELINE_TIER1_REGRESSION_SCHEDULE_MANAGED_KEY})"
            ),
            "default_inputs": {
                "managed_schedule_key": TURN_PIPELINE_TIER1_REGRESSION_SCHEDULE_MANAGED_KEY,
                "case_set": regression_case_set,
                "max_cases": regression_case_limit,
            },
        },
    }


def _is_managed_schedule(schedule: WorkflowSchedule, desired: dict[str, Any]) -> bool:
    if schedule.workflow_id != desired["workflow_id"]:
        return False
    inputs = schedule.default_inputs if isinstance(schedule.default_inputs, dict) else {}
    marker = str(inputs.get("managed_schedule_key") or "").strip()
    if marker == str(desired["managed_schedule_key"]):
        return True
    return str(desired["managed_schedule_key"]) in str(schedule.description or "")


def _matches_desired(schedule: WorkflowSchedule, desired: dict[str, Any]) -> bool:
    inputs = schedule.default_inputs if isinstance(schedule.default_inputs, dict) else {}
    desired_inputs = desired.get("default_inputs") or {}
    return (
        schedule.workflow_id == desired["workflow_id"]
        and schedule.schedule_type == ScheduleType.INTERVAL
        and int(schedule.interval_seconds or 0) == int(desired["interval_seconds"])
        and str(schedule.user_id or "") == str(desired["user_id"])
        and str(schedule.org_id or "") == str(desired["org_id"])
        and str(schedule.namespace or "") == str(desired["namespace"])
        and inputs == desired_inputs
    )


def _ensure_managed_schedule(desired: dict[str, Any]) -> dict[str, Any]:
    manager = get_instance_manager()
    schedules = manager.list_schedules(limit=500)
    managed = [item for item in schedules if _is_managed_schedule(item, desired)]
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
            desired["workflow_id"],
            interval_seconds=int(desired["interval_seconds"]),
            user_id=str(desired["user_id"]),
            org_id=str(desired["org_id"]),
            namespace=str(desired["namespace"]),
            default_inputs=dict(desired["default_inputs"]),
            description=str(desired["description"]),
            start_at=datetime.now(timezone.utc),
        )
        active_schedule_id = manager.create_schedule(schedule)
        created_count += 1

    return {
        "success": True,
        "schedule_name": str(desired["schedule_name"]),
        "workflow_id": str(desired["workflow_id"]),
        "managed_schedule_key": str(desired["managed_schedule_key"]),
        "interval_seconds": int(desired["interval_seconds"]),
        "namespace": str(desired["namespace"]),
        "active_schedule_id": active_schedule_id,
        "created_count": created_count,
        "updated_count": updated_count,
        "disabled_count": disabled_count,
    }


def ensure_turn_pipeline_monitoring_schedules() -> dict[str, Any]:
    """Ensure managed schedules exist for turn-pipeline monitoring surfaces."""

    global _bootstrap_completed

    if not _env_flag("VON_TURN_PIPELINE_MONITORING_SCHEDULE_ENABLE", default=True):
        _bootstrap_completed = True
        return {
            "success": True,
            "ensured": False,
            "reason": "schedule_bootstrap_disabled",
            "created_count": 0,
            "updated_count": 0,
            "disabled_count": 0,
            "schedule_reports": {},
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
                "schedule_reports": {},
            }

        desired_configs = _desired_schedule_configs()
        schedule_reports = {
            name: _ensure_managed_schedule(desired)
            for name, desired in desired_configs.items()
        }

        created_count = sum(
            int((report or {}).get("created_count") or 0)
            for report in schedule_reports.values()
            if isinstance(report, dict)
        )
        updated_count = sum(
            int((report or {}).get("updated_count") or 0)
            for report in schedule_reports.values()
            if isinstance(report, dict)
        )
        disabled_count = sum(
            int((report or {}).get("disabled_count") or 0)
            for report in schedule_reports.values()
            if isinstance(report, dict)
        )
        _bootstrap_completed = True
        return {
            "success": True,
            "ensured": True,
            "created_count": created_count,
            "updated_count": updated_count,
            "disabled_count": disabled_count,
            "schedule_reports": schedule_reports,
            "active_schedule_ids": {
                name: report.get("active_schedule_id")
                for name, report in schedule_reports.items()
                if isinstance(report, dict)
            },
        }


__all__ = [
    "TURN_PIPELINE_MONITORING_SCHEDULE_MANAGED_KEY",
    "TURN_PIPELINE_TIER1_REGRESSION_SCHEDULE_MANAGED_KEY",
    "ensure_turn_pipeline_monitoring_schedules",
]
