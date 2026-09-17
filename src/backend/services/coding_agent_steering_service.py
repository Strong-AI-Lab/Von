"""Controller-owned active-turn observations and exact direct-message admission.

This operational registry is not task authority. Every read checks the live
assignment; the existing owner rechecks the exact binding before dispatch.
Only an authenticated in-process controller publishes, never a browser/model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..db.mongo_client import get_db
from ..security.access_control import get_effective_user_concept_id

COLLECTION = "coding_agent_active_turns"
TARGET_KEYS = {
    "agent_id",
    "organisation_id",
    "task_id",
    "attempt",
    "thread_id",
    "turn_id",
}
# Three missed 15-second observations plus conservative scheduler/store headroom.
OWNER_OBSERVATION_SECONDS = 180


def _collection():
    db = get_db()
    if db is None:
        raise RuntimeError("Coding owner observation store unavailable")
    return db[COLLECTION]


def _task(binding, delegator):
    from . import task_management_service as tasks
    from .task_dispatch_authority_service import (
        has_assignment_record,
        is_authorised_assignment,
    )

    try:
        task = tasks.get_task(binding["task_id"])
    except tasks.TaskNotFoundError as exc:
        raise PermissionError("Coding task is no longer visible") from exc
    if (
        not task
        or task.get("status") != "in_progress"
        or task.get("assignee_concept_id") != binding["agent_id"]
    ):
        raise PermissionError("Coding attempt is no longer active for this actor")
    if task.get("organisation_concept_id") not in (None, binding["organisation_id"]):
        raise PermissionError("Coding task organisation changed")
    # Reuse the canonical assignment-authority decision used by the worker.
    legacy_local_assignment = (
        not task.get("federation")
        and task.get("organisation_concept_id") == binding["organisation_id"]
        and task.get("created_by_concept_id") == delegator
        and not has_assignment_record(binding["task_id"])
    )
    if not legacy_local_assignment and not (
        task.get("dispatch_requested") and is_authorised_assignment(task, delegator)
    ):
        raise PermissionError("Sender is not the coding task delegator")
    attempts = (task.get("execution_timing") or {}).get("attempts", [])
    attempt = next(
        (a for a in attempts if a.get("attempt_id") == binding["attempt"]), None
    )
    if (
        not attempt
        or attempt != attempts[-1]
        or attempt.get("ended_at")
        or attempt.get("outcome")
        or attempt.get("actor_concept_id") != binding["agent_id"]
    ):
        raise PermissionError("Coding execution attempt is not current")
    return task


def publish(binding, *, delegator_id):
    if (
        not isinstance(binding, dict)
        or set(binding) != TARGET_KEYS
        or not all(isinstance(v, str) and v for v in binding.values())
    ):
        raise ValueError("An exact coding turn binding is required")
    if get_effective_user_concept_id() != binding["agent_id"]:
        raise PermissionError("Only the bound coding owner can publish its turn")
    _task(binding, delegator_id)
    now = datetime.now(UTC)
    record = {
        "_id": binding["agent_id"] + ":" + binding["organisation_id"],
        "binding": binding,
        "delegator_id": delegator_id,
        "observed_at": now,
        "expires_at": now + timedelta(seconds=OWNER_OBSERVATION_SECONDS),
    }
    _collection().replace_one({"_id": record["_id"]}, record, upsert=True)
    found = _collection().find_one({"_id": record["_id"]})
    if not found or found["binding"] != binding:
        raise RuntimeError("Coding target canonical read-back failed")
    return binding


def withdraw(binding):
    if get_effective_user_concept_id() != binding["agent_id"]:
        raise PermissionError("Only the coding owner can withdraw its turn")
    _collection().delete_one(
        {
            "_id": binding["agent_id"] + ":" + binding["organisation_id"],
            "binding": binding,
        }
    )


def resolve(*, sender_id, recipient_id, organisation_id):
    from .message_service import (
        DirectMessageSteeringUnavailable,
        authorise_direct_message_participants,
    )

    if get_effective_user_concept_id() != sender_id:
        raise PermissionError("Steering lookup requires the authenticated sender")
    allowed, _ = authorise_direct_message_participants(
        sender_id=sender_id,
        recipient_ids=[recipient_id],
        organisation_concept_id=organisation_id,
    )
    if not allowed:
        raise DirectMessageSteeringUnavailable(
            "No active steering target is available in this message context"
        )
    row = _collection().find_one(
        {
            "_id": recipient_id + ":" + organisation_id,
            "delegator_id": sender_id,
            "expires_at": {"$gt": datetime.now(UTC)},
        }
    )
    if row:
        try:
            _task(row["binding"], sender_id)
            return dict(row["binding"])
        except (PermissionError, ValueError):
            pass
    raise DirectMessageSteeringUnavailable(
        "Active steering is unavailable. Draft retained; choose Queue for separate work."
    )


def validate_submission(*, sender_id, recipient_ids, organisation_id, target):
    from .message_service import DirectMessageSteeringUnavailable

    if len(recipient_ids) != 1 or not organisation_id:
        raise DirectMessageSteeringUnavailable(
            "Steering requires one coding recipient in a shared organisation"
        )
    current = resolve(
        sender_id=sender_id,
        recipient_id=recipient_ids[0],
        organisation_id=organisation_id,
    )
    if target != current:
        raise DirectMessageSteeringUnavailable(
            "The intended coding turn changed. Draft retained; review the current target before retrying."
        )
    return current


def record_delivery(message_id, binding, status):
    from . import message_service as messages
    from ..db.repositories.concepts_repository import ConceptsRepository

    if (
        status not in {"accepted", "not_applied", "uncertain"}
        or get_effective_user_concept_id() != binding["agent_id"]
    ):
        raise PermissionError("Only the owning controller can record delivery")
    message = messages.get_message_for_user(message_id, binding["agent_id"])
    if (
        not message
        or messages.project_direct_message(message).get("submit_target") != binding
    ):
        raise PermissionError("Message target does not match the controller receipt")
    receipt = {
        "status": status,
        "target": binding,
        "observed_at": datetime.now(UTC).isoformat(),
        "consumption_verified": False,
    }
    ConceptsRepository.update_one(
        {"concept_id": message_id},
        {"$set": {"concept_data.steering_delivery": receipt}},
    )
    result = messages.get_message_for_user(message_id, binding["agent_id"])
    if (result or {}).get("concept_data", {}).get("steering_delivery") != receipt:
        raise RuntimeError("Steering receipt canonical read-back failed")
    return receipt
