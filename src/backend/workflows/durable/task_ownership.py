"""Scope-bound durable task identity and trusted action-dispatch fencing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

TASK_OWNERSHIP_CAPABILITY = "durable_task_ownership_v1"
TASK_OWNERSHIP_INDEX = "active_task_owner_unique"


def task_key(document: Mapping[str, Any]) -> str:
    if document.get("task_ownership_key"):
        return document["task_ownership_key"]
    inputs = document.get("inputs") or {}
    task = inputs.get("task_concept_id") or inputs.get("task_id")
    identity = (
        ["task", task]
        if isinstance(task, str) and task.strip()
        else (
            ["schedule", document["schedule_id"]]
            if document.get("schedule_id")
            else (
                ["event", document["event_idempotency_key"]]
                if document.get("event_idempotency_key")
                else ["instance", document["instance_id"]]
            )
        )
    )
    scope = [document.get("user_id"), document.get("org_id"), document.get("namespace")]
    return hashlib.sha256(
        json.dumps([scope, identity], sort_keys=True).encode()
    ).hexdigest()


def ensure_task_ownership_index(instances: Any) -> None:
    partial_filter = {
        "task_ownership_active": True,
        "task_ownership_key": {"$type": "string"},
    }
    existing = instances.index_information().get(TASK_OWNERSHIP_INDEX)
    if existing is not None:
        if (existing.get("unique") is not True
            or existing.get("key") != [("task_ownership_key", 1)]
            or existing.get("partialFilterExpression") != partial_filter):
            raise RuntimeError("durable_task_ownership_index_mismatch")
        return
    instances.create_index(
        [("task_ownership_key", 1)],
        name=TASK_OWNERSHIP_INDEX,
        unique=True,
        partialFilterExpression=partial_filter,
    )


@dataclass(frozen=True)
class TaskClaim:
    manager: Any
    instance_id: str
    worker_id: str
    token: str

    def assert_current(self) -> None:
        if not self.manager.is_current_claim(
            self.instance_id, self.worker_id, self.token
        ):
            raise RuntimeError("durable_task_claim_lost")


_CLAIM: ContextVar[TaskClaim | None] = ContextVar("durable_task_claim", default=None)


@contextmanager
def bind_task_claim(manager: Any, instance_id: str, worker_id: str, token: str | None):
    # Supervised execution has its own authority path and no background lease.
    binding = _CLAIM.set(
        TaskClaim(manager, instance_id, worker_id, token) if token else None
    )
    try:
        yield
    finally:
        _CLAIM.reset(binding)


def assert_current_task_claim() -> None:
    claim = _CLAIM.get()
    if claim is not None:
        claim.assert_current()


def ownership_checked_handler(
    handler: Callable[..., Any], *, method_name: str, category: str
):
    """Check inside the transport thread, after queueing and before invocation."""

    def invoke(**payload: Any):
        claim = _CLAIM.get()
        if claim is None:
            return handler(**payload)
        claim.assert_current()
        effect_id = None
        if category != "read":
            effect_id = claim.manager.begin_claim_effect(
                claim.instance_id, claim.worker_id, claim.token, method_name, payload
            )
        result = handler(**payload)
        if effect_id is not None:
            claim.manager.observe_claim_effect(claim.instance_id, effect_id, result)
        return result

    return invoke


def claim_admission_validator(worker_pattern: str = "^worker_") -> dict[str, Any]:
    """Database-enforced compatibility fence for legacy claim implementations.

    This rejects the observed old code which replaces claimed_by_build on each
    claim. It is compatibility enforcement, not protection against a malicious
    database writer spoofing capabilities or bypassing document validation.
    """
    return {
        "$nor": [
            {
                "status": "running",
                "locked_by": {"$regex": worker_pattern},
                "claimed_by_build.capabilities": {"$ne": TASK_OWNERSHIP_CAPABILITY},
            }
        ]
    }
