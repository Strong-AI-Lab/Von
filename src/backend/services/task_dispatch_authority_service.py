"""Current actor authority for deliberate coding-task assignment.

Historical creators and copied assignments are data. These operational receipts
are never exported with a project and do not replace task visibility or home
checks. Only the canonical assignment service records them.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from ..db.mongo_client import get_db
from ..security.access_control import get_effective_user_concept_id

COLLECTION = "task_dispatch_authorities"


def _collection():
    database = get_db()
    if database is None:
        raise RuntimeError("Task dispatch authority store unavailable")
    return database[COLLECTION]


def record_assignment(task: dict) -> dict | None:
    actor = get_effective_user_concept_id()
    if not actor:
        revoke_assignment(task["task_concept_id"])
        return None
    task_id = task["task_concept_id"]
    receipt = {
        "_id": task_id,
        "dispatch_id": uuid.uuid4().hex,
        "actor_concept_id": actor,
        "assignee_concept_id": task.get("assignee_concept_id"),
        "project_concept_id": task.get("project_concept_id"),
        "organisation_concept_id": task.get("organisation_concept_id"),
        "recorded_at": datetime.now(UTC),
    }
    _collection().replace_one({"_id": task_id}, receipt, upsert=True)
    return receipt


def revoke_assignment(task_id: str) -> None:
    # Keep the requirement for a fresh assignment even if editable task
    # metadata is replaced or its federation marker is removed.
    _collection().update_one(
        {"_id": task_id},
        {"$set": {"actor_concept_id": None, "revoked": True}},
        upsert=True,
    )


def has_assignment_record(task_id: str) -> bool:
    return _collection().find_one({"_id": task_id}, {"_id": 1}) is not None


def is_authorised_assignment(task: dict, actor_id: str) -> bool:
    receipt = _collection().find_one({"_id": task["task_concept_id"]})
    return bool(
        receipt
        and receipt.get("actor_concept_id") == actor_id
        and all(
            receipt.get(field) == task.get(field)
            for field in (
                "assignee_concept_id",
                "project_concept_id",
                "organisation_concept_id",
            )
        )
    )
