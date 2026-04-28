"""Managed schedule bootstrap for multilingual concept-enrichment rumination."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import threading
from typing import Any

from .namespace_service import coerce_namespace, derive_namespace_for_actor
from .multilingual_concept_enrichment_vontology_service import (
    MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
)
from ..workflows.durable.models import ScheduleType, WorkflowSchedule
from ..workflows.durable.multilingual_concept_enrichment_workflow import (
    DEFAULT_MAX_MUTATIONS_PER_RUN,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_DESCRIPTION_CHARS,
    DEFAULT_MIN_TOTAL_USAGE,
    DEFAULT_REANALYSE_AFTER_HOURS,
    DEFAULT_SCAN_LIMIT,
    DEFAULT_TARGET_LANGUAGES,
)
from ..workflows.durable.startup import get_instance_manager

logger = logging.getLogger(__name__)

MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_MANAGED_KEY = (
    "multilingual_concept_enrichment_background_v1"
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


def _target_languages_from_env() -> list[str]:
    raw = os.getenv("VON_MULTILINGUAL_CONCEPT_ENRICHMENT_TARGET_LANGUAGES")
    if not raw:
        return list(DEFAULT_TARGET_LANGUAGES)
    requested = [item.strip() for item in raw.split(",") if item.strip()]
    allowed = set(DEFAULT_TARGET_LANGUAGES)
    selected = [item for item in requested if item in allowed]
    return selected or list(DEFAULT_TARGET_LANGUAGES)


def _desired_schedule_config() -> dict[str, Any]:
    user_id = str(
        os.getenv("VON_MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_USER_ID", "#V#system")
    ).strip() or "#V#system"
    org_id = str(
        os.getenv("VON_MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_ORG_ID", "#V#default")
    ).strip() or "#V#default"
    namespace_override = coerce_namespace(
        os.getenv("VON_MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_NAMESPACE")
    )
    namespace = (
        namespace_override
        or derive_namespace_for_actor(user_id, org_id)
        or "#V#system"
    )

    interval_seconds = _coerce_int(
        os.getenv("VON_MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_INTERVAL_SECONDS"),
        default=86400,
        minimum=1800,
        maximum=604800,
    )
    scan_limit = _coerce_int(
        os.getenv("VON_MULTILINGUAL_CONCEPT_ENRICHMENT_SCAN_LIMIT"),
        default=DEFAULT_SCAN_LIMIT,
        minimum=1,
        maximum=200,
    )
    max_mutations = _coerce_int(
        os.getenv("VON_MULTILINGUAL_CONCEPT_ENRICHMENT_MAX_MUTATIONS_PER_RUN"),
        default=DEFAULT_MAX_MUTATIONS_PER_RUN,
        minimum=1,
        maximum=100,
    )
    min_confidence = _coerce_float(
        os.getenv("VON_MULTILINGUAL_CONCEPT_ENRICHMENT_MIN_CONFIDENCE"),
        default=DEFAULT_MIN_CONFIDENCE,
        minimum=0.5,
        maximum=0.999,
    )
    minimum_total_usage = _coerce_int(
        os.getenv("VON_MULTILINGUAL_CONCEPT_ENRICHMENT_MINIMUM_TOTAL_USAGE"),
        default=DEFAULT_MIN_TOTAL_USAGE,
        minimum=1,
        maximum=100000,
    )
    min_description_chars = _coerce_int(
        os.getenv("VON_MULTILINGUAL_CONCEPT_ENRICHMENT_MIN_DESCRIPTION_CHARS"),
        default=DEFAULT_MIN_DESCRIPTION_CHARS,
        minimum=20,
        maximum=5000,
    )
    reanalyse_after_hours = _coerce_int(
        os.getenv("VON_MULTILINGUAL_CONCEPT_ENRICHMENT_REANALYSE_AFTER_HOURS"),
        default=DEFAULT_REANALYSE_AFTER_HOURS,
        minimum=1,
        maximum=24 * 365,
    )
    target_languages = _target_languages_from_env()

    default_inputs = {
        "managed_schedule_key": MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_MANAGED_KEY,
        "scan_limit": scan_limit,
        "max_mutations_per_run": max_mutations,
        "min_confidence": min_confidence,
        "minimum_total_usage": minimum_total_usage,
        "min_description_chars": min_description_chars,
        "reanalyse_after_hours": reanalyse_after_hours,
        "target_languages": target_languages,
    }

    return {
        "user_id": user_id,
        "org_id": org_id,
        "namespace": namespace,
        "interval_seconds": interval_seconds,
        "default_inputs": default_inputs,
    }


def _is_managed_multilingual_schedule(schedule: WorkflowSchedule) -> bool:
    if schedule.workflow_id != MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID:
        return False
    inputs = schedule.default_inputs if isinstance(schedule.default_inputs, dict) else {}
    marker = str(inputs.get("managed_schedule_key") or "").strip()
    if marker == MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_MANAGED_KEY:
        return True
    return MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_MANAGED_KEY in str(
        schedule.description or ""
    )


def _matches_desired(schedule: WorkflowSchedule, desired: dict[str, Any]) -> bool:
    inputs = schedule.default_inputs if isinstance(schedule.default_inputs, dict) else {}
    desired_inputs = desired.get("default_inputs") or {}
    return (
        schedule.workflow_id == MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID
        and schedule.schedule_type == ScheduleType.INTERVAL
        and int(schedule.interval_seconds or 0) == int(desired["interval_seconds"])
        and str(schedule.user_id or "") == str(desired["user_id"])
        and str(schedule.org_id or "") == str(desired["org_id"])
        and str(schedule.namespace or "") == str(desired["namespace"])
        and str(inputs.get("managed_schedule_key") or "")
        == str(desired_inputs.get("managed_schedule_key") or "")
        and int(inputs.get("scan_limit") or 0)
        == int(desired_inputs.get("scan_limit") or 0)
        and int(inputs.get("max_mutations_per_run") or 0)
        == int(desired_inputs.get("max_mutations_per_run") or 0)
        and float(inputs.get("min_confidence") or 0.0)
        == float(desired_inputs.get("min_confidence") or 0.0)
        and int(inputs.get("minimum_total_usage") or 0)
        == int(desired_inputs.get("minimum_total_usage") or 0)
        and int(inputs.get("min_description_chars") or 0)
        == int(desired_inputs.get("min_description_chars") or 0)
        and int(inputs.get("reanalyse_after_hours") or 0)
        == int(desired_inputs.get("reanalyse_after_hours") or 0)
        and list(inputs.get("target_languages") or [])
        == list(desired_inputs.get("target_languages") or [])
    )


def ensure_multilingual_concept_enrichment_background_schedule() -> dict[str, Any]:
    """Ensure the managed multilingual enrichment background schedule exists."""

    global _bootstrap_completed

    if not _env_flag(
        "VON_MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_ENABLE",
        default=True,
    ):
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
        managed = [
            item for item in schedules if _is_managed_multilingual_schedule(item)
        ]
        matching = [item for item in managed if _matches_desired(item, desired)]

        created_count = 0
        updated_count = 0
        disabled_count = 0
        active_schedule_id: str | None = None

        if matching:
            primary = sorted(matching, key=lambda item: item.schedule_id)[0]
            active_schedule_id = primary.schedule_id
            if not primary.enabled and manager.set_schedule_enabled(
                primary.schedule_id,
                True,
            ):
                updated_count += 1
            for duplicate in matching[1:]:
                if duplicate.enabled and manager.set_schedule_enabled(
                    duplicate.schedule_id,
                    False,
                ):
                    disabled_count += 1
            for mismatch in managed:
                if mismatch.schedule_id == primary.schedule_id:
                    continue
                if mismatch.enabled and manager.set_schedule_enabled(
                    mismatch.schedule_id,
                    False,
                ):
                    disabled_count += 1
        else:
            for mismatch in managed:
                if mismatch.enabled and manager.set_schedule_enabled(
                    mismatch.schedule_id,
                    False,
                ):
                    disabled_count += 1

            schedule = WorkflowSchedule.create_interval(
                MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
                interval_seconds=int(desired["interval_seconds"]),
                user_id=str(desired["user_id"]),
                org_id=str(desired["org_id"]),
                namespace=str(desired["namespace"]),
                default_inputs=dict(desired["default_inputs"]),
                description=(
                    "Managed multilingual concept-enrichment background schedule "
                    f"({MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_MANAGED_KEY})"
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
            "managed_schedule_key": MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_MANAGED_KEY,
            "workflow_id": MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
            "interval_seconds": int(desired["interval_seconds"]),
            "namespace": str(desired["namespace"]),
            "target_languages": list(
                (desired.get("default_inputs") or {}).get("target_languages") or []
            ),
        }


__all__ = [
    "MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_MANAGED_KEY",
    "ensure_multilingual_concept_enrichment_background_schedule",
]
