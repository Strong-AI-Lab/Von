"""Coding execution observations on the canonical task, independent of archives.

Controllers supply observed process/consumer times, never queue/import times.
Attempt IDs are the existing controller IDs; compare-and-set preserves retries.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

from . import coding_execution_usage as usage
from .task_project_home_service import guard_task_write

KEY = "execution_timing"


def timestamp(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Execution timestamps require an explicit UTC offset")
    return parsed.astimezone(UTC).isoformat()


def elapsed(start, end):
    if not start or not end:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def project(metadata, status):
    timing = deepcopy(metadata.get(KEY) or {})
    attempts = timing.get("attempts", [])
    starts = [a["started_at"] for a in attempts if a.get("started_at")]
    completions = [
        {"attempt_id": a["attempt_id"], "completed_at": a["completion_accepted_at"]}
        for a in attempts
        if a.get("completion_accepted_at")
    ]
    completions.sort(key=lambda row: row["completed_at"])
    started = min(starts) if starts else None
    completed = (
        completions[-1]["completed_at"]
        if completions
        and status in {"completed", "deployed"}
        and any(
            a.get("completion_accepted_at") == completions[-1]["completed_at"]
            and a.get("completion_transition_count")
            == (
                metadata.get("task_status_transition_count", 0) - (status == "deployed")
            )
            for a in attempts
        )
        else None
    )
    for attempt in attempts:
        attempt["elapsed_wall_seconds"] = elapsed(
            attempt.get("started_at"), attempt.get("ended_at")
        )
    return {
        "started_at": started,
        "completed_at": completed,
        "elapsed_wall_seconds": elapsed(started, completed),
        "duration_basis": "elapsed wall clock, including waits; not active coding time",
        "attempts": attempts,
        "completion_history": completions,
        "usage": usage.project(attempts),
        "cost": {
            "status": "unknown",
            "amount": None,
            "currency": None,
            "reason": "Subscription usage is not a per-task invoice; no attributable price receipt.",
        },
    }


def _change(task_id, change):
    from . import task_management_service as tasks

    for _ in range(8):
        task_id, doc = tasks._get_task_doc(task_id)
        previous = (doc.get("metadata") or {}).get(KEY)
        timing = deepcopy(previous or {"attempts": []})
        change(timing, doc)
        if timing == previous:
            return tasks.get_task(task_id)
        result = tasks.ConceptsRepository.update_one(
            {
                "concept_id": task_id,
                f"metadata.{KEY}": previous,
                "metadata.task_status_transition_count": (
                    doc.get("metadata") or {}
                ).get("task_status_transition_count"),
            },
            {"$set": {f"metadata.{KEY}": timing}},
        )
        if result.matched_count:
            return tasks.get_task(task_id)
    raise RuntimeError("Concurrent execution timing update; retry the retained receipt")


def _attempt(timing, attempt_id):
    return next((a for a in timing["attempts"] if a["attempt_id"] == attempt_id), None)


def _actor(task, actor):
    from ..security.access_control import get_effective_user_concept_id

    trusted = get_effective_user_concept_id()
    if (
        not actor
        or (trusted and trusted != actor)
        or task.get("assignee_concept_id") != actor
    ):
        raise PermissionError("Execution timing must be recorded by the assigned actor")


@guard_task_write
def record_usage(task_concept_id, *, attempt_id, actor_concept_id, receipt):
    """Persist controller-observed usage independently of timing acceptance."""
    from . import task_management_service as tasks

    usage.validate(receipt)
    _actor(tasks.get_task(task_concept_id), actor_concept_id)

    def apply(timing, doc):
        attempt = _attempt(timing, attempt_id)
        if not attempt or attempt["actor_concept_id"] != actor_concept_id:
            raise ValueError("Usage requires the actor's existing execution attempt")
        prior = attempt.get("usage")
        if prior and prior != receipt and prior["coverage"] != "unknown":
            raise ValueError("Attempt already has a different usage receipt")
        attempt["usage"] = deepcopy(receipt)

    return _change(task_concept_id, apply)


@guard_task_write
def start(task_concept_id, *, attempt_id, actor_concept_id, started_at, source):
    from . import task_management_service as tasks

    if not isinstance(attempt_id, str) or not attempt_id.strip() or not source:
        raise ValueError("An existing controller attempt ID and source are required")
    started_at = timestamp(started_at)
    if not started_at:
        raise ValueError("Observed execution start is required")
    task = tasks.get_task(task_concept_id)
    _actor(task, actor_concept_id)

    def apply(timing, doc):
        existing = _attempt(timing, attempt_id)
        if existing:
            if (
                existing["actor_concept_id"] != actor_concept_id
                or existing["source"] != source
            ):
                raise ValueError("Attempt already bound to another actor/source")
            # Resume/replayed begin must retain the original observed start.
            return
        if task["status"] not in {"pending", "in_progress", "blocked"}:
            raise ValueError("Task is not open for execution")
        timing["attempts"].append(
            {
                "attempt_id": attempt_id,
                "actor_concept_id": actor_concept_id,
                "source": source,
                "started_at": started_at,
            }
        )

    return _change(task_concept_id, apply)


@guard_task_write
def finish(task_concept_id, *, attempt_id, actor_concept_id, outcome, ended_at=None):
    """Accept a retained result, reconcile status, then persist canonical acceptance.

    A crash after status transition is retried with the same attempt. An already
    accepted attempt cannot complete a reopened task. Missing exit times stay
    unknown; result acceptance is a separate observed timestamp.
    """
    from . import task_management_service as tasks

    if outcome not in {"completed", "blocked", "needs_input", "failed"}:
        raise ValueError("Invalid execution outcome")
    ended_at = timestamp(ended_at)
    task = tasks.get_task(task_concept_id)
    _actor(task, actor_concept_id)

    def retain(timing, doc):
        attempt = _attempt(timing, attempt_id)
        if not attempt or attempt["actor_concept_id"] != actor_concept_id:
            raise ValueError("Execution attempt start was not recorded")
        if attempt.get("outcome"):
            if attempt["outcome"] != outcome or attempt.get("ended_at") != ended_at:
                raise ValueError("Attempt already has a different terminal receipt")
            return
        if task["status"] not in {"pending", "in_progress", "blocked"}:
            raise ValueError("Task became terminal before this result was retained")
        if ended_at and elapsed(attempt["started_at"], ended_at) < 0:
            raise ValueError("Execution end precedes start")
        attempt.update(
            outcome=outcome,
            ended_at=ended_at,
            result_base_transition_count=(doc.get("metadata") or {}).get(
                "task_status_transition_count", 0
            ),
        )

    task = _change(task_concept_id, retain)
    attempt = _attempt(task[KEY], attempt_id)
    if attempt.get("result_accepted_at"):
        return task
    target = "completed" if outcome == "completed" else "blocked"
    if task["status"] not in {"pending", "in_progress", "blocked", target}:
        raise ValueError("Task changed before execution result acceptance")
    _, doc = tasks._get_task_doc(task_concept_id)
    transitions = (doc.get("metadata") or {}).get("task_status_transition_count", 0)
    base = attempt["result_base_transition_count"]
    if transitions > base + 1 or (transitions == base + 1 and task["status"] != target):
        raise ValueError("Task reopened or changed after the retained execution result")
    expected_transitions = transitions + (task["status"] != target)
    tasks.update_task_status(task_concept_id, target, actor_concept_id=actor_concept_id)
    if tasks.get_task(task_concept_id)["status"] != target:
        raise RuntimeError("Execution result status read-back failed")

    def accept(timing, doc):
        if (doc.get("metadata") or {}).get(
            "task_status_transition_count", 0
        ) != expected_transitions:
            raise ValueError("Task status changed before timing acceptance")
        attempt = _attempt(timing, attempt_id)
        if not attempt.get("result_accepted_at"):
            attempt["result_accepted_at"] = datetime.now(UTC).isoformat()
            if outcome == "completed":
                attempt["completion_accepted_at"] = attempt["result_accepted_at"]
                attempt["completion_transition_count"] = (
                    doc.get("metadata") or {}
                ).get("task_status_transition_count", 0)

    return _change(task_concept_id, accept)
