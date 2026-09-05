"""Actor-owned schedule commands shared by chat and HTTP/Workflow Studio.

Identity is resolved by the trusted entry point. This service validates the
actor-visible executable and cadence before creating state, then returns the
persisted schedule. It uses the existing Vontology schedule repository and
durable occurrence deduplication; a schedule is not a passive task.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .workflow_actor_scope_service import (
    WorkflowActorScope,
    WorkflowActorScopeError,
    resolve_authoritative_workflow_actor_scope,
)
from ..workflows.durable.models import WorkflowSchedule
from ..workflows.durable.scheduler import (
    _calculate_next_cron_run,
    _parse_cron_expression,
)
from ..workflows.durable.workflow_instance_submission_service import (
    _resolve_submission_launch_inputs,
    verify_workflow_runnable,
)
from ..workflows import workflow_listing_service


class ScheduleCommandError(ValueError):
    def __init__(self, code: str, detail: str, status: int = 400):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(detail)


def _scope_values(scope: WorkflowActorScope) -> dict[str, Any]:
    if not scope.user_concept_id or not scope.namespace:
        raise ScheduleCommandError(
            "workflow_actor_authority_required",
            "Authenticated schedule owner required.",
            403,
        )
    return {
        "user_id": scope.user_concept_id,
        "org_id": scope.organisation_concept_id,
        "namespace": scope.namespace,
    }


def get_owned_schedule(
    manager: Any, schedule_id: str, scope: WorkflowActorScope
) -> WorkflowSchedule:
    owner = _scope_values(scope)
    schedule = manager.get_schedule(schedule_id)
    matches = schedule is not None
    if schedule is not None:
        try:
            resolve_authoritative_workflow_actor_scope(
                claimed_user_id=schedule.user_id,
                claimed_org_id=schedule.org_id,
                claimed_namespace=schedule.namespace,
                allow_unscoped_claims=False,
                ambient_user_id=owner["user_id"],
                ambient_org_id=owner["org_id"],
                ambient_context_supplied=True,
            )
        except WorkflowActorScopeError:
            matches = False
    if (
        not matches
        or schedule.workflow_id
        not in workflow_listing_service.filter_workflow_ids_for_current_actor(
            [schedule.workflow_id]
        )
    ):
        raise ScheduleCommandError(
            "schedule_not_found", "Schedule not found in the current actor scope.", 404
        )
    return schedule


def schedule_receipt(schedule: WorkflowSchedule, **outcome: Any) -> dict[str, Any]:
    receipt = {
        "success": True,
        **schedule.to_status_dict(),
        "user_id": schedule.user_id,
        "org_id": schedule.org_id,
        "namespace": schedule.namespace,
        "default_inputs": schedule.default_inputs,
        **outcome,
    }
    if "changed" in outcome:
        receipt["effect_status"] = "succeeded"
        receipt["canonical_read_back"] = schedule.to_status_dict()
    return receipt


def prepare_actor_schedule(
    command: Mapping[str, Any],
    scope: WorkflowActorScope,
    *,
    request_id: str | None = None,
    conversation_id: str | None = None,
) -> WorkflowSchedule:
    owner = _scope_values(scope)
    workflow_id = str(command.get("workflow_id") or "").strip()
    if (
        not workflow_id
        or workflow_id
        not in workflow_listing_service.filter_workflow_ids_for_current_actor(
            [workflow_id]
        )
    ):
        raise ScheduleCommandError(
            "workflow_not_found", "Workflow not found in the current actor scope.", 404
        )
    inputs = command.get("default_inputs")
    if inputs is None:
        inputs = {}
    if not isinstance(inputs, dict):
        raise ScheduleCommandError(
            "invalid_launch_inputs", "default_inputs must be an object."
        )
    inputs = dict(inputs)
    verification = verify_workflow_runnable(
        workflow_id, actor_user_id=owner["user_id"], actor_org_id=owner["org_id"]
    )
    if not verification.runnable_verification_success:
        raise ScheduleCommandError(
            "workflow_not_runnable",
            "The selected workflow is not executable in this actor scope.",
        )
    definition, resolution = _resolve_submission_launch_inputs(
        workflow_id=workflow_id,
        inputs=inputs,
        actor_user_id=owner["user_id"],
        actor_org_id=owner["org_id"],
        actor_namespace=owner["namespace"],
    )
    if definition is None or resolution.unresolved_required_inputs:
        raise ScheduleCommandError(
            "invalid_launch_inputs",
            "Required workflow inputs unresolved: "
            + ", ".join(resolution.unresolved_required_inputs),
        )
    metadata = getattr(definition, "metadata", None)
    lifecycle = (
        metadata.get("publication_lifecycle") if isinstance(metadata, Mapping) else None
    )
    if isinstance(lifecycle, Mapping) and lifecycle.get("published") is False:
        raise ScheduleCommandError(
            "workflow_not_published", "The selected workflow is explicitly unpublished."
        )
    inputs.update(resolution.resolved_inputs)
    # Match the canonical submission projection; payload aliases cannot become
    # another actor's execution identity at the deferred handoff.
    inputs.update(
        user_concept_id=owner["user_id"],
        org_concept_id=owner["org_id"],
        organisation_concept_id=owner["org_id"],
        namespace=owner["namespace"],
        user_namespace=owner["namespace"],
    )
    now = datetime.now(timezone.utc)
    kind = str(command.get("schedule_type") or "interval").strip().lower()
    common = {
        **owner,
        "default_inputs": inputs,
        "description": command.get("description"),
    }
    cadence: dict[str, Any] = {"schedule_type": kind}
    if kind == "interval":
        seconds = command.get("interval_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            raise ScheduleCommandError(
                "invalid_cadence", "interval_seconds must be a positive integer."
            )
        cadence["interval_seconds"] = seconds
        schedule = WorkflowSchedule.create_interval(
            workflow_id, seconds, start_at=now + timedelta(seconds=seconds), **common
        )
    elif kind == "once":
        try:
            run_at = datetime.fromisoformat(
                str(command.get("run_at") or "").replace("Z", "+00:00")
            )
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)
            run_at = run_at.astimezone(timezone.utc)
        except ValueError as exc:
            raise ScheduleCommandError(
                "invalid_cadence", "run_at must be an ISO datetime with a timezone."
            ) from exc
        cadence["run_at"] = run_at.isoformat()
        schedule = WorkflowSchedule.create_once(workflow_id, run_at, **common)
    elif kind == "cron":
        expression = " ".join(str(command.get("cron_expression") or "").split())
        fields = _parse_cron_expression(expression)
        bounds = {
            "minute": (0, 59),
            "hour": (0, 23),
            "day": (1, 31),
            "month": (1, 12),
            "weekday": (0, 6),
        }
        if fields is None or any(
            values is not None
            and (
                not values
                or min(values) < bounds[key][0]
                or max(values) > bounds[key][1]
            )
            for key, values in (fields or {}).items()
        ):
            raise ScheduleCommandError(
                "invalid_cadence",
                "Invalid five-field cron cadence (UTC, weekday 0=Monday).",
            )
        next_run = _calculate_next_cron_run(fields, now)
        components = {
            "minute": next_run.minute,
            "hour": next_run.hour,
            "day": next_run.day,
            "month": next_run.month,
            "weekday": next_run.weekday(),
        }
        if any(
            values is not None and components[key] not in values
            for key, values in fields.items()
        ):
            raise ScheduleCommandError(
                "invalid_cadence",
                "Cron cadence has no occurrence in the scheduler's next-year horizon.",
            )
        cadence["cron_expression"] = expression
        schedule = WorkflowSchedule.create_cron(workflow_id, expression, **common)
        schedule.next_run_at = next_run
    else:
        raise ScheduleCommandError(
            "invalid_cadence", "schedule_type must be once, interval or cron."
        )
    identity = dict(verification.definition_identity or {})
    key = str(command.get("idempotency_key") or request_id or "").strip()
    digest = hashlib.sha256(
        json.dumps(
            {
                "actor": owner,
                "definition": identity,
                "workflow_id": workflow_id,
                "cadence": cadence,
                "inputs": inputs,
                "caller_key": key,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    schedule.schedule_id = digest
    schedule.origin = "actor_owned"
    schedule.definition_identity = identity
    schedule.creation_context = {
        "request_id": request_id,
        "conversation_id": conversation_id,
        "intent_hash": digest,
    }
    return schedule


def create_actor_schedule(
    manager: Any, command: Mapping[str, Any], scope: WorkflowActorScope, **context: Any
) -> dict[str, Any]:
    proposed = prepare_actor_schedule(command, scope, **context)
    schedule_id = f"#V#schedule_{proposed.schedule_id}"

    def confirmed_receipt(*, changed: bool) -> dict[str, Any]:
        saved = get_owned_schedule(manager, schedule_id, scope)
        fields = (
            "workflow_id",
            "schedule_type",
            "run_at",
            "interval_seconds",
            "cron_expression",
            "default_inputs",
            "origin",
            "definition_identity",
        )
        if any(getattr(saved, key) != getattr(proposed, key) for key in fields):
            raise ScheduleCommandError(
                "schedule_readback_mismatch",
                "Schedule read-back did not confirm the requested configuration.",
                503,
            )
        return schedule_receipt(saved, changed=changed, idempotent_replay=not changed)

    existing = manager.get_schedule(schedule_id)
    if existing is not None:
        return confirmed_receipt(changed=False)
    try:
        created_id = manager.create_schedule(proposed)
    except Exception as exc:
        # The canonical concept identity is the cross-process uniqueness point.
        # An in-flight/failed peer creation must never cause a second identity.
        existing = manager.get_schedule(schedule_id)
        if existing is not None and existing.enabled and existing.next_run_at:
            return confirmed_receipt(changed=False)
        raise ScheduleCommandError(
            "schedule_creation_unconfirmed",
            "Schedule creation was not confirmed; retry the same request.",
            503,
        ) from exc
    if created_id != schedule_id:
        raise ScheduleCommandError(
            "schedule_readback_mismatch",
            "Schedule creation returned an unexpected identity.",
            503,
        )
    return confirmed_receipt(changed=True)


def set_owned_schedule_enabled(
    manager: Any, schedule_id: str, enabled: bool, scope: WorkflowActorScope
) -> dict[str, Any]:
    if not isinstance(enabled, bool):
        raise ScheduleCommandError("invalid_enabled", "enabled must be true or false.")
    before = get_owned_schedule(manager, schedule_id, scope)
    changed = before.enabled != enabled
    if changed and not manager.set_schedule_enabled(schedule_id, enabled):
        raise ScheduleCommandError(
            "schedule_update_unconfirmed", "Schedule state could not be updated.", 503
        )
    saved = get_owned_schedule(manager, schedule_id, scope)
    if saved.enabled != enabled:
        raise ScheduleCommandError(
            "schedule_readback_mismatch",
            "Schedule read-back did not confirm its enabled state.",
            503,
        )
    return schedule_receipt(saved, changed=changed, idempotent_replay=not changed)
