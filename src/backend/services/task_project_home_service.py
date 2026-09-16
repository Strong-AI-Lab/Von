"""Explicit task-project write homes and a recoverable cutover barrier.

Home routing is operational state, separate from Jira/Von tracking authority.
An interrupted admitted write retains its token: elapsed time cannot certify
that its effects stopped. Operator transitions require an empty operation set.
This service does not copy execution leases or authorise a task's execution.
"""

from __future__ import annotations

import os
import uuid
import inspect
from functools import wraps
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

COLLECTION = "task_project_homes"
_ADMITTED: ContextVar[frozenset[str]] = ContextVar(
    "task_project_home_admitted", default=frozenset()
)


class TaskProjectHomeError(ValueError):
    def __init__(self, code: str, route: dict | None = None):
        self.code = code
        self.route = route or {}
        super().__init__(
            f"{code}: use task project home {self.route.get('home_url', 'recorded in its route')}"
        )


def _collection():
    from ..db.mongo_client import get_db

    db = get_db()
    if db is None:
        raise TaskProjectHomeError("task_home_unavailable")
    return db[COLLECTION]


def local_node_id() -> str:
    return os.environ.get("VON_TASK_HOME_NODE_ID", "").strip()


def get_project_home(project_id: str) -> dict | None:
    row = _collection().find_one({"_id": project_id})
    if row is None:
        return None
    return {key: value for key, value in row.items() if key not in {"operations"}}


def guard_task_write(function):
    """Share one admission across nested canonical task mutations."""
    signature = inspect.signature(function)

    @wraps(function)
    def guarded(*args, **kwargs):
        from .task_management_service import _get_task_doc
        from ..security.access_control import can_access_concept

        arguments = signature.bind(*args, **kwargs).arguments
        project_ids = {arguments.get("project_concept_id")}
        fields = arguments.get("fields") or {}
        if isinstance(fields, dict):
            project_ids.add(fields.get("project_concept_id"))
        task_ids = set()
        for key, value in arguments.items():
            if key in {
                "task_concept_id",
                "source_task_concept_id",
                "target_task_concept_id",
                "parent_task_concept_id",
                "epic_task_concept_id",
                "repair_task_id",
                "task_concept_ids",
                "task_ids",
            }:
                task_ids.update(
                    [value]
                    if isinstance(value, str)
                    else value if isinstance(value, (list, tuple)) else []
                )
        if task_ids:
            docs = [_get_task_doc(task_id)[1] for task_id in sorted(task_ids)]
            project_ids.update(
                (doc.get("metadata") or {}).get("project_concept_id") for doc in docs
            )
        with ExitStack() as stack:
            for project_id in sorted(project_ids - {None, ""}):
                if not can_access_concept(project_id):
                    raise PermissionError("Task project is not accessible")
                stack.enter_context(project_write(project_id))
            return function(*args, **kwargs)

    return guarded


def register_project_home(
    *,
    project_id: str,
    node_id: str,
    home_url: str,
    source_identity: dict,
    evidence: dict,
) -> dict:
    """Operator-only initial registration; never overwrite a prior route."""
    if (
        not project_id
        or not node_id
        or not home_url
        or not source_identity
        or not evidence
    ):
        raise ValueError("Project, home, source identity and evidence are required")
    row = {
        "_id": project_id,
        "schema_version": "task_project_home.v1",
        "home_node_id": node_id,
        "home_url": home_url,
        "epoch": 1,
        "state": "active",
        "source_identity": source_identity,
        "evidence": evidence,
        "operations": {},
        "updated_at": datetime.now(UTC),
    }
    try:
        _collection().insert_one(row)
    except DuplicateKeyError:
        existing = get_project_home(project_id)
        if any(existing.get(k) != row[k] for k in ("home_node_id", "source_identity")):
            raise TaskProjectHomeError("task_home_registration_conflict", existing)
    return get_project_home(project_id)


@contextmanager
def project_write(project_id: str | None):
    """Admit a canonical mutation at the local home, before any task effects."""
    if not project_id or project_id in _ADMITTED.get():
        yield
        return
    collection = _collection()
    home = collection.find_one({"_id": project_id})
    if home is None:
        # Existing projects remain compatible until an explicit registration.
        yield
        return
    if home.get("state") != "active" or home.get("home_node_id") != local_node_id():
        raise TaskProjectHomeError("task_project_read_only_replica", home)
    operation_id = uuid.uuid4().hex
    admitted = collection.find_one_and_update(
        {
            "_id": project_id,
            "epoch": home["epoch"],
            "state": "active",
            "home_node_id": local_node_id(),
        },
        {
            "$set": {
                f"operations.{operation_id}": {
                    "started_at": datetime.now(UTC),
                    "pid": os.getpid(),
                }
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if admitted is None:
        raise TaskProjectHomeError(
            "task_home_changed_before_write", get_project_home(project_id)
        )
    token = _ADMITTED.set(_ADMITTED.get() | {project_id})
    completed = False
    try:
        yield
        completed = True
    finally:
        _ADMITTED.reset(token)
        # A killed process or an uncertain failed write retains its token.
        # Reconcile its effects before certifying the project quiescent.
        if completed:
            collection.update_one(
                {"_id": project_id, "epoch": home["epoch"]},
                {"$unset": {f"operations.{operation_id}": ""}},
            )


def reconcile_project_operation(
    *, project_id: str, operation_id: str, expected_operation: dict, evidence: dict
) -> bool:
    """Operator recovery after independently checking process and effect state."""
    if not evidence.get("process_drained") or not evidence.get("effects_reconciled"):
        raise ValueError("Drain and canonical effect reconciliation are required")
    if not operation_id.isalnum() or not expected_operation:
        raise ValueError("Exact retained operation required")
    result = _collection().update_one(
        {"_id": project_id, f"operations.{operation_id}": expected_operation},
        {
            "$unset": {f"operations.{operation_id}": ""},
            "$push": {
                "operation_reconciliations": {
                    "operation_id": operation_id,
                    "evidence": evidence,
                    "at": datetime.now(UTC),
                }
            },
        },
    )
    return bool(result.matched_count)


def freeze_project_home(
    *, project_id: str, expected_epoch: int, evidence: dict
) -> dict:
    if not evidence:
        raise ValueError("Freeze evidence is required")
    row = _collection().find_one_and_update(
        {
            "_id": project_id,
            "epoch": expected_epoch,
            "state": "active",
            "home_node_id": local_node_id(),
            "operations": {},
        },
        {
            "$set": {
                "state": "frozen",
                "freeze_evidence": evidence,
                "updated_at": datetime.now(UTC),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if row is None:
        current = get_project_home(project_id)
        if (
            current
            and current.get("state") == "frozen"
            and current.get("epoch") == expected_epoch
        ):
            return current
        raise TaskProjectHomeError("task_home_not_quiescent_or_changed", current)
    return get_project_home(project_id)


def transfer_frozen_home(
    *,
    project_id: str,
    expected_epoch: int,
    node_id: str,
    home_url: str,
    snapshot_digest: str,
    receipt: dict,
) -> dict:
    """Fence this prior home after a verified destination receipt.

    Recovery never restores older task bytes. Returning the home requires a new
    verified snapshot containing all changes accepted by its intervening home.
    """
    if (
        not node_id
        or not home_url
        or len(snapshot_digest) != 64
        or not receipt.get("canonical_readback")
    ):
        raise ValueError("A verified destination snapshot receipt is required")
    row = _collection().find_one_and_update(
        {
            "_id": project_id,
            "epoch": expected_epoch,
            "state": "frozen",
            "operations": {},
        },
        {
            "$set": {
                "home_node_id": node_id,
                "home_url": home_url,
                "state": "replica",
                "snapshot_digest": snapshot_digest,
                "transfer_receipt": receipt,
                "updated_at": datetime.now(UTC),
            },
            "$inc": {"epoch": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    if row is None:
        current = get_project_home(project_id)
        if current and all(
            current.get(k) == v
            for k, v in {
                "home_node_id": node_id,
                "state": "replica",
                "snapshot_digest": snapshot_digest,
                "epoch": expected_epoch + 1,
            }.items()
        ):
            return current
        raise TaskProjectHomeError("task_home_transfer_conflict", current)
    return get_project_home(project_id)
