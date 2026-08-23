"""Bootstrap managed schedules for email-source representation convergence."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any

from ..workflows.durable.models import ScheduleType, WorkflowSchedule
from ..workflows.durable.startup import get_instance_manager
from .email_source_representation_convergence_workflow_vontology_service import (
    ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID,
)
from .namespace_service import coerce_namespace, derive_namespace_for_actor

EMAIL_ARXIV_REPRESENTATION_CONVERGENCE_SCHEDULE_MANAGED_KEY = (
    "email_paper_representation_convergence_v3"
)
_LEGACY_MANAGED_KEYS = frozenset(
    {
        "email_arxiv_representation_convergence_v1",
        "email_arxiv_representation_convergence_v2",
    }
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


def _clean_env(name: str, default: str) -> str:
    return str(os.getenv(name, default) or default).strip() or default


def _resolve_schedule_identity() -> dict[str, str]:
    user_id = _clean_env(
        "VON_EMAIL_ARXIV_CONVERGENCE_SCHEDULE_USER_ID",
        "#V#michael_witbrock",
    )
    org_id = _clean_env(
        "VON_EMAIL_ARXIV_CONVERGENCE_SCHEDULE_ORG_ID",
        "university_of_auckland_strong_ai_lab",
    )
    namespace_override = coerce_namespace(
        os.getenv("VON_EMAIL_ARXIV_CONVERGENCE_SCHEDULE_NAMESPACE")
    )
    namespace = namespace_override or derive_namespace_for_actor(user_id, org_id)
    return {
        "user_id": user_id,
        "org_id": org_id,
        "namespace": namespace
        or "#V#michael_witbrock@university_of_auckland_strong_ai_lab",
    }


def _desired_schedule_config() -> dict[str, Any]:
    identity = _resolve_schedule_identity()
    interval_seconds = _coerce_int(
        os.getenv("VON_EMAIL_ARXIV_CONVERGENCE_INTERVAL_SECONDS"),
        default=3600,
        minimum=300,
        maximum=604800,
    )
    gmail_max_results = _coerce_int(
        os.getenv("VON_EMAIL_ARXIV_CONVERGENCE_MAX_RESULTS"),
        default=3,
        minimum=1,
        maximum=20,
    )
    gmail_profile = _clean_env(
        "VON_EMAIL_ARXIV_CONVERGENCE_GMAIL_PROFILE",
        "#V#gmail_profile_vonwitbrock_gmail",
    )
    base_gmail_query = _clean_env(
        "VON_EMAIL_ARXIV_CONVERGENCE_BASE_QUERY",
        "{arxiv.org doi.org paper preprint manuscript}",
    )
    return {
        **identity,
        "workflow_id": ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID,
        "interval_seconds": interval_seconds,
        "default_inputs": {
            "managed_schedule_key": (
                EMAIL_ARXIV_REPRESENTATION_CONVERGENCE_SCHEDULE_MANAGED_KEY
            ),
            "trigger_source": "email_arxiv_representation_convergence_schedule",
            "prompt": (
                "Process the newest Inbox messages mentioning scientific papers "
                "that do not have VON/PAPER/REPRESENTED. Treat a bare identifiable "
                "paper mention as representation intent, represent every paper, "
                "verify current processing evidence, then apply the represented "
                "label and remove INBOX."
            ),
            "gmail_profile": gmail_profile,
            "base_gmail_query": base_gmail_query,
            "gmail_max_results": gmail_max_results,
            "user_concept_id": identity["user_id"],
            "namespace": identity["namespace"],
            "org_concept_id": identity["org_id"],
        },
    }


def _is_managed_schedule(schedule: WorkflowSchedule, desired: dict[str, Any]) -> bool:
    if schedule.workflow_id != desired["workflow_id"]:
        return False
    inputs = (
        schedule.default_inputs if isinstance(schedule.default_inputs, dict) else {}
    )
    marker = str(inputs.get("managed_schedule_key") or "").strip()
    known_keys = {
        EMAIL_ARXIV_REPRESENTATION_CONVERGENCE_SCHEDULE_MANAGED_KEY,
        *_LEGACY_MANAGED_KEYS,
    }
    if marker in known_keys:
        return True
    description = str(schedule.description or "")
    return any(key in description for key in known_keys)


def _matches_desired(schedule: WorkflowSchedule, desired: dict[str, Any]) -> bool:
    inputs = (
        schedule.default_inputs if isinstance(schedule.default_inputs, dict) else {}
    )
    return (
        schedule.workflow_id == desired["workflow_id"]
        and schedule.schedule_type == ScheduleType.INTERVAL
        and int(schedule.interval_seconds or 0) == int(desired["interval_seconds"])
        and str(schedule.user_id or "") == str(desired["user_id"])
        and str(schedule.org_id or "") == str(desired["org_id"])
        and str(schedule.namespace or "") == str(desired["namespace"])
        and inputs == desired["default_inputs"]
        and schedule.next_run_at is not None
    )


def ensure_email_arxiv_representation_convergence_schedule(
    *,
    activate: bool | None = None,
) -> dict[str, Any]:
    """Ensure a managed interval schedule exists for recurring paper-email runs."""

    global _bootstrap_completed

    if (
        not _env_flag("VON_EMAIL_ARXIV_CONVERGENCE_SCHEDULE_ENABLE", default=True)
        and activate is not True
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
        if _bootstrap_completed and activate is None:
            return {
                "success": True,
                "ensured": True,
                "reason": "already_bootstrapped",
                "created_count": 0,
                "updated_count": 0,
                "disabled_count": 0,
            }

        desired = _desired_schedule_config()
        should_activate = (
            bool(activate)
            if activate is not None
            else _env_flag(
                "VON_EMAIL_ARXIV_CONVERGENCE_SCHEDULE_AUTO_ACTIVATE",
                default=False,
            )
        )
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
            if should_activate and not primary.enabled and manager.set_schedule_enabled(
                primary.schedule_id, True
            ):
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
                description=(
                    "Managed email arXiv representation convergence schedule "
                    f"({EMAIL_ARXIV_REPRESENTATION_CONVERGENCE_SCHEDULE_MANAGED_KEY})"
                ),
                start_at=datetime.now(timezone.utc),
            )
            schedule.enabled = should_activate
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
            "managed_schedule_key": (
                EMAIL_ARXIV_REPRESENTATION_CONVERGENCE_SCHEDULE_MANAGED_KEY
            ),
            "workflow_id": desired["workflow_id"],
            "interval_seconds": int(desired["interval_seconds"]),
            "namespace": str(desired["namespace"]),
            "gmail_profile": desired["default_inputs"]["gmail_profile"],
            "gmail_max_results": int(desired["default_inputs"]["gmail_max_results"]),
            "schedule_enabled": bool(
                should_activate
                or (
                    matching
                    and bool(
                        next(
                            item.enabled
                            for item in matching
                            if item.schedule_id == active_schedule_id
                        )
                    )
                )
            ),
        }


__all__ = [
    "EMAIL_ARXIV_REPRESENTATION_CONVERGENCE_SCHEDULE_MANAGED_KEY",
    "ensure_email_arxiv_representation_convergence_schedule",
]
