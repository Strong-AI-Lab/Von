"""Server-side persistence for chat prompts queued behind active turns.

This service is a support surface: it stores and transitions prompt queue
records, but it does not decide what Von should answer or how workflows run.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

from pymongo import ASCENDING, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from ..db.mongo_client import (
    get_chat_prompt_queue_collection,
    get_chat_prompt_queue_counters_collection,
)
from .namespace_service import (
    concept_id_to_namespace_slug,
    derive_actor_context_from_namespace,
    resolve_canonical_namespace,
)

STATUS_QUEUED = "queued"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_IN_PROGRESS)
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)
CANCELLABLE_STATUSES = ACTIVE_STATUSES + (STATUS_FAILED,)
VALID_STATUSES = set(ACTIVE_STATUSES + TERMINAL_STATUSES)
MAX_PROMPT_RAW_CHARS = 100_000
MAX_SESSION_NAME_CHARS = 500
STALE_IN_PROGRESS_TIMEOUT_SECONDS = 24 * 60 * 60
RECENT_FAILED_VISIBILITY_SECONDS = 24 * 60 * 60
DEFAULT_DISPATCH_LEASE_SECONDS = 90
DEFAULT_HANDOFF_RECONCILIATION_RETRY_SECONDS = 5
DEFAULT_TASK_EXECUTION_RECONCILIATION_LEASE_SECONDS = 90
DEFAULT_TERMINAL_RETENTION_SECONDS = 30 * 24 * 60 * 60
DEFAULT_TERMINAL_BACKFILL_GRACE_SECONDS = 7 * 24 * 60 * 60
DISPATCH_MODE_SERVER = "server"
TASK_EXECUTION_RECONCILIATION_PENDING = "pending"
TASK_EXECUTION_RECONCILIATION_CLAIMED = "claimed"
TASK_EXECUTION_RECONCILIATION_ACKNOWLEDGED = "acknowledged"
EXECUTION_ENVELOPE_VERSION = 1
MAX_EXECUTION_ENVELOPE_CHARS = 100_000
EXECUTION_ENVELOPE_ALLOWED_FIELDS = frozenset(
    {
        "initiation_id",
        "language",
        "model",
        "model_parameters",
        "model_provider",
        "presenter_mode",
        "skip_buttonify",
        "thinking_card_mode",
        "turn_kind",
        "workflow_inputs",
        "image_attachment_ids",
    }
)
STALE_IN_PROGRESS_ADVISORY_REASON = (
    "Prompt queue record has remained in progress for more than 24 hours; "
    "reconcile its exact turn or task receipt before retrying or terminalising it."
)
# Retained for compatibility with persisted historical failures and callers that
# display their original diagnostic text. Age alone no longer creates this error.
STALE_IN_PROGRESS_LAST_ERROR = (
    "Prompt queue record expired after being in progress for more than 24 hours."
)
LEGACY_SUBMISSION_ROLE_QUEUE = "queue"
LEGACY_SUBMISSION_ROLE_GENERATE = "generate"
LEGACY_SUBMISSION_ROLES = {
    LEGACY_SUBMISSION_ROLE_QUEUE,
    LEGACY_SUBMISSION_ROLE_GENERATE,
}
LEGACY_SUBMISSION_RENDEZVOUS_SECONDS = 5 * 60
_LEGACY_VONTOLOGY_NON_TRIGGER_PREFIX_RE = re.compile(r"#([Vv])\u200B#")


class ChatPromptQueueError(RuntimeError):
    """Base error for chat prompt queue operations."""


class ChatPromptQueueUnavailable(ChatPromptQueueError):
    """Raised when the persistence backend is unavailable."""


class InvalidChatPromptQueueInput(ChatPromptQueueError, ValueError):
    """Raised when a caller supplies invalid queue data."""


class ChatPromptQueueRecordNotFound(ChatPromptQueueError, LookupError):
    """Raised when a queue record is not in the expected scope/state."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "not_found_or_wrong_state",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = dict(details or {})


class ConversationTurnAlreadyActive(ChatPromptQueueError):
    """Raised when another admitted turn owns the conversation carrier."""

    def __init__(self, message: str, *, queue_id: str | None = None) -> None:
        super().__init__(message)
        self.queue_id = queue_id


class ChatPromptQueueCapacityReached(ChatPromptQueueError):
    """Raised when the bounded foreground backlog has no free slot."""

    def __init__(self, message: str, *, limit_kind: str) -> None:
        super().__init__(message)
        self.limit_kind = limit_kind


class ChatPromptQueueSubmissionConflict(InvalidChatPromptQueueInput):
    """Raised when one enqueue identity is reused for different work."""

    error_code = "enqueue_submission_conflict"
    status_code = 409

    def __init__(self, message: str, *, queue_id: str | None = None) -> None:
        super().__init__(message)
        self.queue_id = queue_id


class ChatPromptQueueTaskAlreadyActive(ChatPromptQueueError):
    """Raised when another active queue row already executes the same task."""

    error_code = "task_execution_already_active"
    status_code = 409

    def __init__(
        self,
        message: str,
        *,
        queue_id: str | None = None,
        task_execution_concept_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.queue_id = queue_id
        self.task_execution_concept_id = task_execution_concept_id


class ChatPromptQueueReconciliationRequired(InvalidChatPromptQueueInput):
    """Raised when an ambiguous server-owned attempt needs exact evidence."""

    error_code = "server_dispatch_reconciliation_required"
    status_code = 409


class ChatPromptQueueHandoffConflict(InvalidChatPromptQueueInput):
    """Raised when a queued replacement cannot safely retire its source row."""

    error_code = "queue_handoff_conflict"
    status_code = 409

    def __init__(self, message: str, *, queue_id: str | None = None) -> None:
        super().__init__(message)
        self.queue_id = queue_id


class ChatPromptQueueRetryConflict(InvalidChatPromptQueueInput):
    """Raised when a failed row cannot accept a linked retry successor."""

    error_code = "queue_retry_conflict"
    status_code = 409

    def __init__(self, message: str, *, queue_id: str | None = None) -> None:
        super().__init__(message)
        self.queue_id = queue_id


_QUEUE_ADMISSION_LOCK = threading.RLock()
_UNSET = object()


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _foreground_queue_limits() -> tuple[int, int]:
    return (
        _positive_int_env("VON_MAX_QUEUED_TURNS_GLOBAL", 500),
        _positive_int_env("VON_MAX_QUEUED_TURNS_PER_USER", 25),
    )


def _dispatch_lease_seconds(value: int | None = None) -> int:
    if value is not None:
        try:
            return max(1, int(value))
        except (TypeError, ValueError) as exc:
            raise InvalidChatPromptQueueInput(
                "lease_seconds must be a positive integer"
            ) from exc
    return _positive_int_env(
        "VON_CHAT_PROMPT_QUEUE_DISPATCH_LEASE_SECONDS",
        DEFAULT_DISPATCH_LEASE_SECONDS,
    )


def _turn_lease_seconds(value: int | None = None) -> int:
    if value is not None:
        try:
            return max(1, int(value))
        except (TypeError, ValueError) as exc:
            raise InvalidChatPromptQueueInput(
                "lease_seconds must be a positive integer"
            ) from exc
    return _positive_int_env(
        "VON_CHAT_PROMPT_QUEUE_TURN_LEASE_SECONDS",
        DEFAULT_DISPATCH_LEASE_SECONDS,
    )


def _terminal_retention_seconds() -> int:
    return _positive_int_env(
        "VON_CHAT_PROMPT_QUEUE_TERMINAL_RETENTION_SECONDS",
        DEFAULT_TERMINAL_RETENTION_SECONDS,
    )


def _terminal_backfill_grace_seconds(value: int | None = None) -> int:
    if value is not None:
        try:
            return max(1, int(value))
        except (TypeError, ValueError) as exc:
            raise InvalidChatPromptQueueInput(
                "rollout_grace_seconds must be a positive integer"
            ) from exc
    return _positive_int_env(
        "VON_CHAT_PROMPT_QUEUE_TERMINAL_BACKFILL_GRACE_SECONDS",
        DEFAULT_TERMINAL_BACKFILL_GRACE_SECONDS,
    )


def _terminal_purge_after(completed_at: datetime) -> datetime:
    return completed_at + timedelta(seconds=_terminal_retention_seconds())


def _task_execution_reconciliation_lease_seconds(value: int | None = None) -> int:
    if value is not None:
        try:
            return max(1, int(value))
        except (TypeError, ValueError) as exc:
            raise InvalidChatPromptQueueInput(
                "lease_seconds must be a positive integer"
            ) from exc
    return _positive_int_env(
        "VON_TASK_EXECUTION_RECONCILIATION_LEASE_SECONDS",
        DEFAULT_TASK_EXECUTION_RECONCILIATION_LEASE_SECONDS,
    )


def _has_task_execution_link(doc: Mapping[str, Any] | None) -> bool:
    if not isinstance(doc, Mapping):
        return False
    return any(
        isinstance(doc.get(field), str) and bool(str(doc.get(field)).strip())
        for field in ("task_concept_id", "task_execution_concept_id")
    )


def _terminal_reconciliation_update(
    doc: Mapping[str, Any] | None,
    *,
    status: str,
    error: str | None,
    source: str,
    observed_now: datetime,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Build the retention/reconciliation half of one terminal queue CAS.

    A linked queue row cannot acquire a TTL date until its exact TaskExecution
    terminal projection has been acknowledged.  The pending marker is written
    in the same Mongo update as the queue terminal state, so a process failure
    cannot leave a terminal row that looks fully reconciled.
    """

    if not _has_task_execution_link(doc):
        return {"purge_after": _terminal_purge_after(observed_now)}, {}
    return (
        {
            "task_execution_reconciliation_status": (
                TASK_EXECUTION_RECONCILIATION_PENDING
            ),
            "task_execution_reconciliation_queue_status": status,
            "task_execution_reconciliation_error": error,
            "task_execution_reconciliation_source": source,
            "task_execution_reconciliation_pending_at": observed_now,
            "task_execution_reconciliation_updated_at": observed_now,
        },
        {
            "purge_after": "",
            "task_execution_reconciliation_token": "",
            "task_execution_reconciliation_owner": "",
            "task_execution_reconciliation_acquired_at": "",
            "task_execution_reconciliation_heartbeat_at": "",
            "task_execution_reconciliation_lease_expires_at": "",
            "task_execution_reconciliation_next_at": "",
            "task_execution_reconciliation_last_error": "",
            "task_execution_reconciled_at": "",
        },
    )


def _queued_user_slot_key(user_concept_id: str, slot: int) -> str:
    actor_hash = hashlib.sha256(user_concept_id.encode("utf-8")).hexdigest()
    return f"{actor_hash}:{slot}"


def _active_task_execution_key(task_concept_id: str) -> str:
    return hashlib.sha256(task_concept_id.encode("utf-8")).hexdigest()


def _enqueue_sort() -> list[tuple[str, int]]:
    """Sort legacy rows first, then all new rows by their durable sequence."""

    return [("enqueue_sequence", 1), ("created_at", 1), ("queue_id", 1)]


def _earlier_enqueue_filter(record: Mapping[str, Any]) -> dict[str, Any]:
    """Match rows allocated before ``record``, including pre-sequence legacy rows."""

    sequence = record.get("enqueue_sequence")
    if isinstance(sequence, int) and not isinstance(sequence, bool):
        return {
            "$or": [
                {"enqueue_sequence": {"$exists": False}},
                {"enqueue_sequence": {"$lt": sequence}},
            ]
        }
    created_at = record.get("created_at") or _now()
    queue_id = str(record.get("queue_id") or "")
    return {
        "enqueue_sequence": {"$exists": False},
        "$or": [
            {"created_at": {"$lt": created_at}},
            {"created_at": created_at, "queue_id": {"$lt": queue_id}},
        ],
    }


def _raise_if_task_execution_already_active(
    *,
    coll: Collection,
    active_task_execution_key: str | None,
) -> None:
    if not active_task_execution_key:
        return
    existing = coll.find_one(
        {"active_task_execution_key": active_task_execution_key},
        {"queue_id": 1, "task_execution_concept_id": 1},
    )
    if not isinstance(existing, Mapping):
        return
    raise ChatPromptQueueTaskAlreadyActive(
        "This task already has an active Von execution",
        queue_id=str(existing.get("queue_id") or "") or None,
        task_execution_concept_id=(
            str(existing.get("task_execution_concept_id") or "") or None
        ),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_text(
    value: Any, *, field: str, required: bool, max_chars: int | None = None
) -> str | None:
    if value is None:
        if required:
            raise InvalidChatPromptQueueInput(f"{field} is required")
        return None
    text = str(value)
    if required and not text.strip():
        raise InvalidChatPromptQueueInput(f"{field} is required")
    if max_chars is not None and len(text) > max_chars:
        raise InvalidChatPromptQueueInput(f"{field} is too long")
    return text


def _coerce_scope_value(value: Any, *, field: str, required: bool = True) -> str | None:
    text = _coerce_text(value, field=field, required=required, max_chars=2_000)
    if text is None:
        return None
    cleaned = text.strip()
    if required and not cleaned:
        raise InvalidChatPromptQueueInput(f"{field} is required")
    return cleaned or None


def _normalise_execution_envelope(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidChatPromptQueueInput("execution_envelope must be an object")
    invalid_keys = sorted(
        str(key)
        for key in value
        if not isinstance(key, str) or key not in EXECUTION_ENVELOPE_ALLOWED_FIELDS
    )
    if invalid_keys:
        raise InvalidChatPromptQueueInput(
            "execution_envelope contains invalid keys: " + ", ".join(invalid_keys[:8])
        )
    for mapping_field in ("model_parameters", "workflow_inputs"):
        nested = value.get(mapping_field)
        if nested is not None and not isinstance(nested, Mapping):
            raise InvalidChatPromptQueueInput(
                f"execution_envelope.{mapping_field} must be an object"
            )
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise InvalidChatPromptQueueInput(
            "execution_envelope must contain JSON values"
        ) from exc
    if len(encoded) > MAX_EXECUTION_ENVELOPE_CHARS:
        raise InvalidChatPromptQueueInput("execution_envelope is too large")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - guarded above
        raise InvalidChatPromptQueueInput("execution_envelope must be an object")
    return decoded


def _enqueue_fingerprint(
    *,
    scope: Mapping[str, Any],
    prompt_raw: str,
    session_id: str | None,
    session_name: str | None,
    conversation_key: str,
    execution_envelope: Mapping[str, Any],
    execution_envelope_version: int,
    task_concept_id: str | None,
    task_execution_concept_id: str | None,
    dispatch_ready: bool,
    handoff_source_queue_id: str | None,
    retry_source_queue_id: str | None,
) -> str:
    material = {
        "schema": "chat_prompt_queue_enqueue.v2",
        "scope": _scope_query(scope),
        "prompt_raw": prompt_raw,
        "session_id": session_id,
        "session_name": session_name,
        "conversation_key": conversation_key,
        "execution_envelope": dict(execution_envelope),
        "execution_envelope_version": execution_envelope_version,
        "task_concept_id": task_concept_id,
        "task_execution_concept_id": task_execution_concept_id,
        "dispatch_ready_at_enqueue": dispatch_ready,
        "handoff_source_queue_id": handoff_source_queue_id,
        "retry_source_queue_id": retry_source_queue_id,
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _idempotent_enqueue_result(
    doc: Mapping[str, Any] | None, *, replayed: bool
) -> dict[str, Any] | None:
    record = serialise_queue_record(doc)
    if record is not None:
        record["idempotent_replay"] = replayed
    return record


def _find_idempotent_enqueue(
    *,
    coll: Collection,
    scope: Mapping[str, Any],
    enqueue_submission_id: str,
    enqueue_fingerprint: str,
) -> dict[str, Any] | None:
    existing = coll.find_one(
        {
            **_scope_query(scope),
            "enqueue_submission_id": enqueue_submission_id,
        }
    )
    if not isinstance(existing, Mapping):
        return None
    if existing.get("enqueue_fingerprint") != enqueue_fingerprint:
        raise ChatPromptQueueSubmissionConflict(
            "enqueue_submission_id was already used for different queue work",
            queue_id=str(existing.get("queue_id") or "") or None,
        )
    return _idempotent_enqueue_result(existing, replayed=True)


def _raise_if_handoff_source_already_bound(
    *,
    coll: Collection,
    handoff_source_queue_id: str | None,
) -> None:
    if not handoff_source_queue_id:
        return
    existing = coll.find_one(
        {"handoff_source_queue_id": handoff_source_queue_id},
        {"queue_id": 1},
    )
    if not isinstance(existing, Mapping):
        return
    raise ChatPromptQueueHandoffConflict(
        "The handoff source is already bound to another durable replacement",
        queue_id=str(existing.get("queue_id") or "") or None,
    )


def _find_retry_successor(
    *,
    coll: Collection,
    scope: Mapping[str, Any],
    retry_source_queue_id: str | None,
) -> dict[str, Any] | None:
    if not retry_source_queue_id:
        return None
    existing = coll.find_one(
        {
            **_compatible_scope_query(scope),
            "retry_source_queue_id": retry_source_queue_id,
        }
    )
    if not isinstance(existing, Mapping):
        return None
    return _idempotent_enqueue_result(existing, replayed=True)


def _normalise_user_concept_id(value: Any) -> str:
    cleaned = _coerce_scope_value(value, field="user_concept_id")
    if cleaned is None:  # pragma: no cover - _coerce_scope_value enforces this
        raise InvalidChatPromptQueueInput("user_concept_id is required")
    return cleaned if cleaned.startswith("#V#") else f"#V#{cleaned}"


def _normalise_organisation_component(value: Any) -> str | None:
    cleaned = _coerce_scope_value(
        value,
        field="organisation_concept_id",
        required=False,
    )
    if cleaned is None:
        return None
    return cleaned if cleaned.startswith("#V#") else f"#V#{cleaned}"


def _normalise_namespace_component(
    value: Any,
    *,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
) -> str | None:
    cleaned = _coerce_scope_value(value, field="namespace", required=False)
    return resolve_canonical_namespace(
        cleaned,
        user_concept_id,
        organisation_concept_id,
        preserve_explicit=True,
    )


def _queue_scope_values(scope: Mapping[str, Any]) -> dict[str, str | None]:
    user_id = _normalise_user_concept_id(scope.get("user_concept_id"))
    org_id = _normalise_organisation_component(scope.get("organisation_concept_id"))
    namespace = _normalise_namespace_component(
        scope.get("namespace"),
        user_concept_id=user_id,
        organisation_concept_id=org_id,
    )
    namespace_user_id, namespace_org_id = derive_actor_context_from_namespace(namespace)
    if namespace_user_id and namespace_user_id != user_id:
        namespace = _normalise_namespace_component(
            None,
            user_concept_id=user_id,
            organisation_concept_id=org_id,
        )
    elif org_id is None and namespace_org_id:
        org_id = namespace_org_id
    elif org_id is not None and namespace_org_id and namespace_org_id != org_id:
        namespace = _normalise_namespace_component(
            None,
            user_concept_id=user_id,
            organisation_concept_id=org_id,
        )
    return {
        "user_concept_id": user_id,
        "organisation_concept_id": org_id,
        "namespace": namespace,
    }


def _value_variants(value: str | None, *, field: str) -> list[str | None]:
    if value is None:
        return [None]
    variants: list[str | None] = [value]
    if field == "user_concept_id":
        slug = concept_id_to_namespace_slug(value)
        if slug and slug not in variants:
            variants.append(slug)
    elif field == "organisation_concept_id":
        slug = concept_id_to_namespace_slug(value)
        if slug and slug not in variants:
            variants.append(slug)
    elif field == "namespace":
        slug = value[3:] if value.startswith("#V#") else None
        if slug and slug not in variants:
            variants.append(slug)
        if None not in variants:
            variants.append(None)
    return variants


def _compatible_scope_query(scope: Mapping[str, Any]) -> dict[str, Any]:
    canonical = _scope_query(scope)
    return {
        field: {"$in": _value_variants(value, field=field)}
        for field, value in canonical.items()
    }


def _scope_matches(record: Mapping[str, Any], scope: Mapping[str, Any]) -> bool:
    try:
        canonical = _scope_query(scope)
        record_scope = _scope_query(record)
        return all(
            record_scope_value in _value_variants(canonical_value, field=field)
            for field, canonical_value in canonical.items()
            for record_scope_value in [record_scope.get(field)]
        )
    except InvalidChatPromptQueueInput:
        return False


def _collection() -> Collection:
    coll = get_chat_prompt_queue_collection()
    if coll is None:
        raise ChatPromptQueueUnavailable("chat_prompt_queue collection is unavailable")
    return coll


def _next_enqueue_sequence() -> int:
    """Allocate a durable total order before inserting one new queue row."""

    counters = get_chat_prompt_queue_counters_collection()
    if counters is None:
        raise ChatPromptQueueUnavailable(
            "chat_prompt_queue counter collection is unavailable"
        )
    counter = counters.find_one_and_update(
        {"_id": "global_enqueue_sequence"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    try:
        sequence = int((counter or {}).get("value"))
    except (TypeError, ValueError) as exc:  # pragma: no cover - corrupt store
        raise ChatPromptQueueUnavailable(
            "chat_prompt_queue enqueue sequence is invalid"
        ) from exc
    if sequence < 1:  # pragma: no cover - corrupt store
        raise ChatPromptQueueUnavailable(
            "chat_prompt_queue enqueue sequence is invalid"
        )
    return sequence


def build_queue_scope(
    *,
    user_concept_id: str,
    organisation_concept_id: str | None = None,
    namespace: str | None = None,
) -> dict[str, str | None]:
    """Build the persisted isolation scope for a user's queue."""

    return _queue_scope_values(
        {
            "user_concept_id": user_concept_id,
            "organisation_concept_id": organisation_concept_id,
            "namespace": namespace,
        }
    )


def _build_active_legacy_submission_key(
    *,
    scope: Mapping[str, Any],
    conversation_key: str | None,
    prompt_raw: str,
    window_session_id: str | None,
) -> str | None:
    """Build a content-free idempotency key for one old-client active send."""

    conversation_key_clean = _coerce_scope_value(
        conversation_key,
        field="conversation_key",
        required=False,
    )
    window_session_id_clean = _coerce_scope_value(
        window_session_id,
        field="window_session_id",
        required=False,
    )
    if not conversation_key_clean or not window_session_id_clean:
        return None
    canonical_scope = _scope_query(scope)
    canonical_prompt = _LEGACY_VONTOLOGY_NON_TRIGGER_PREFIX_RE.sub(
        r"#\1#",
        prompt_raw,
    ).strip()
    prompt_digest = hashlib.sha256(canonical_prompt.encode("utf-8")).hexdigest()
    window_session_digest = hashlib.sha256(
        window_session_id_clean.encode("utf-8")
    ).hexdigest()
    material = "\x1f".join(
        (
            "active_legacy_submission.v1",
            str(canonical_scope.get("user_concept_id") or ""),
            str(canonical_scope.get("organisation_concept_id") or ""),
            str(canonical_scope.get("namespace") or ""),
            conversation_key_clean,
            window_session_digest,
            prompt_digest,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _expire_unpaired_legacy_submissions(
    *,
    coll: Collection,
    now: datetime,
) -> int:
    """Remove bounded rendezvous state without changing queue lifecycle truth."""

    result = coll.update_many(
        {
            "active_legacy_submission_key": {"$exists": True},
            "legacy_submission_expires_at": {"$lte": now},
        },
        {
            "$unset": {
                "active_legacy_submission_key": "",
                "legacy_submission_queue_seen": "",
                "legacy_submission_generate_seen": "",
                "legacy_submission_expires_at": "",
            }
        },
    )
    return int(getattr(result, "modified_count", 0) or 0)


def _find_or_join_legacy_submission(
    *,
    coll: Collection,
    scope: Mapping[str, Any],
    active_legacy_submission_key: str,
    role: str,
    client_request_id: str | None,
    now: datetime,
) -> dict[str, Any] | None:
    """Reuse the same half or atomically join the complementary old-client half."""

    existing = coll.find_one(
        {
            **_scope_query(scope),
            "active_legacy_submission_key": active_legacy_submission_key,
            "legacy_submission_expires_at": {"$gt": now},
        }
    )
    if not isinstance(existing, Mapping):
        return None

    role_field = f"legacy_submission_{role}_seen"
    other_role = (
        LEGACY_SUBMISSION_ROLE_GENERATE
        if role == LEGACY_SUBMISSION_ROLE_QUEUE
        else LEGACY_SUBMISSION_ROLE_QUEUE
    )
    other_role_field = f"legacy_submission_{other_role}_seen"
    if existing.get(role_field) is True:
        if role == LEGACY_SUBMISSION_ROLE_GENERATE:
            existing_request_id = _coerce_scope_value(
                existing.get("client_request_id"),
                field="client_request_id",
                required=False,
            )
            if (
                existing_request_id
                and client_request_id
                and existing_request_id != client_request_id
            ):
                if (
                    existing.get("status") in TERMINAL_STATUSES
                    and existing.get(other_role_field) is not True
                ):
                    retired = coll.find_one_and_update(
                        {
                            **_scope_query(scope),
                            "queue_id": str(existing.get("queue_id") or ""),
                            "active_legacy_submission_key": (
                                active_legacy_submission_key
                            ),
                            "status": {"$in": list(TERMINAL_STATUSES)},
                            role_field: True,
                            other_role_field: {"$ne": True},
                        },
                        {
                            "$set": {"legacy_submission_closed_at": now},
                            "$unset": {
                                "active_legacy_submission_key": "",
                                "legacy_submission_queue_seen": "",
                                "legacy_submission_generate_seen": "",
                                "legacy_submission_expires_at": "",
                            },
                        },
                        return_document=ReturnDocument.AFTER,
                    )
                    if retired is not None:
                        return None
                    if (
                        coll.find_one(
                            {
                                **_scope_query(scope),
                                "active_legacy_submission_key": (
                                    active_legacy_submission_key
                                ),
                            },
                            {"_id": 1},
                        )
                        is None
                    ):
                        return None
                raise ConversationTurnAlreadyActive(
                    "another matching turn is active for this conversation",
                    queue_id=str(existing.get("queue_id") or "") or None,
                )
        return serialise_queue_record(existing)

    set_fields: dict[str, Any] = {role_field: True}
    update: dict[str, Any] = {"$set": set_fields}
    if existing.get(other_role_field) is True:
        set_fields["legacy_submission_closed_at"] = now
        update["$unset"] = {
            "active_legacy_submission_key": "",
            "legacy_submission_expires_at": "",
        }
    queue_id = str(existing.get("queue_id") or "")
    joined = coll.find_one_and_update(
        {
            **_scope_query(scope),
            "queue_id": queue_id,
            "active_legacy_submission_key": active_legacy_submission_key,
            role_field: {"$ne": True},
        },
        update,
        return_document=ReturnDocument.AFTER,
    )
    if joined is None:
        # A same-role retry can race the complementary join that closed the
        # unique key. Re-read only the exact row already selected above.
        joined = coll.find_one(
            {
                **_scope_query(scope),
                "queue_id": queue_id,
                role_field: True,
            }
        )
    return serialise_queue_record(joined)


def _scope_query(scope: Mapping[str, Any]) -> dict[str, Any]:
    return _queue_scope_values(scope)


def _transition_not_found_error(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
    expected_statuses: Sequence[str],
    fallback_message: str,
) -> ChatPromptQueueRecordNotFound:
    doc = _collection().find_one({"queue_id": queue_id})
    expected = [status for status in expected_statuses if status in VALID_STATUSES]
    base_details: dict[str, Any] = {
        "queue_id": queue_id,
        "expected_statuses": expected,
    }
    if not doc:
        return ChatPromptQueueRecordNotFound(
            fallback_message,
            error_code="not_found",
            details={**base_details, "exists": False},
        )
    current_status = doc.get("status")
    scope_match = _scope_matches(doc, scope)
    if not scope_match:
        return ChatPromptQueueRecordNotFound(
            "prompt queue record belongs to a different scope",
            error_code="scope_mismatch",
            details={
                **base_details,
                "exists": True,
                "scope_match": False,
                "current_status": current_status,
            },
        )
    return ChatPromptQueueRecordNotFound(
        f"prompt queue record is {current_status or 'unknown'}, not in the expected state",
        error_code="wrong_state",
        details={
            **base_details,
            "exists": True,
            "scope_match": True,
            "current_status": current_status,
        },
    )


def expire_stale_in_progress_records(
    *,
    scope: Mapping[str, Any],
    now: datetime | None = None,
    stale_after_seconds: int = STALE_IN_PROGRESS_TIMEOUT_SECONDS,
) -> int:
    """Mark old in-progress records advisory without changing their outcome.

    Claimed age is not evidence that the represented turn stopped or failed. A
    caller may explicitly cancel or requeue after reconciling the exact task or
    turn receipt, but this maintenance pass must not manufacture that verdict.
    The legacy function name is retained for API compatibility.
    """

    seconds = max(1, int(stale_after_seconds or STALE_IN_PROGRESS_TIMEOUT_SECONDS))
    observed_now = now or _now()
    cutoff = observed_now - timedelta(seconds=seconds)
    result = _collection().update_many(
        {
            **_compatible_scope_query(scope),
            "status": STATUS_IN_PROGRESS,
            "$or": [
                {"lease_expires_at": {"$lte": observed_now}},
                {
                    "lease_expires_at": {"$exists": False},
                    "$or": [
                        {"lease_heartbeat_at": {"$lte": cutoff}},
                        {
                            "lease_heartbeat_at": {"$exists": False},
                            "claimed_at": {"$lte": cutoff},
                        },
                        {
                            "lease_heartbeat_at": {"$exists": False},
                            "claimed_at": None,
                            "updated_at": {"$lte": cutoff},
                        },
                    ],
                },
            ],
        },
        {
            "$set": {
                **_scope_query(scope),
                "stale_advisory": True,
                "stale_advisory_at": observed_now,
                "stale_advisory_reason": STALE_IN_PROGRESS_ADVISORY_REASON,
                "reconciliation_required": True,
            }
        },
    )
    return int(getattr(result, "modified_count", 0) or 0)


def _serialise_datetime(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def serialise_queue_record(doc: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not doc:
        return None
    return {
        "queue_id": doc.get("queue_id"),
        "prompt_raw": doc.get("prompt_raw") or "",
        "status": doc.get("status"),
        "session_id": doc.get("session_id"),
        "session_name": doc.get("session_name"),
        "attempt_count": int(doc.get("attempt_count") or 0),
        "created_at": _serialise_datetime(doc.get("created_at")),
        "updated_at": _serialise_datetime(doc.get("updated_at")),
        "queued_at": _serialise_datetime(doc.get("queued_at")),
        "claimed_at": _serialise_datetime(doc.get("claimed_at")),
        "completed_at": _serialise_datetime(doc.get("completed_at")),
        "last_error": doc.get("last_error"),
        "source": doc.get("source"),
        "enqueue_submission_id": doc.get("enqueue_submission_id"),
        "dispatch_mode": doc.get("dispatch_mode"),
        "dispatch_ready": doc.get("dispatch_ready"),
        "handoff_source_queue_id": doc.get("handoff_source_queue_id"),
        "retired_handoff_source_queue_id": doc.get(
            "retired_handoff_source_queue_id"
        ),
        "handoff_replacement_queue_id": doc.get("handoff_replacement_queue_id"),
        "handoff_source_retired_at": _serialise_datetime(
            doc.get("handoff_source_retired_at")
        ),
        "handoff_completed_at": _serialise_datetime(doc.get("handoff_completed_at")),
        "handoff_cancelled_at": _serialise_datetime(doc.get("handoff_cancelled_at")),
        "handoff_reconciliation_attempted_at": _serialise_datetime(
            doc.get("handoff_reconciliation_attempted_at")
        ),
        "handoff_reconciliation_next_at": _serialise_datetime(
            doc.get("handoff_reconciliation_next_at")
        ),
        "handoff_reconciliation_attempt_count": int(
            doc.get("handoff_reconciliation_attempt_count") or 0
        ),
        "handoff_reconciliation_last_error": doc.get(
            "handoff_reconciliation_last_error"
        ),
        "retry_source_queue_id": doc.get("retry_source_queue_id"),
        "retry_request_id": doc.get("retry_request_id"),
        "retry_source_task_execution_concept_id": doc.get(
            "retry_source_task_execution_concept_id"
        ),
        "retried_by_queue_id": doc.get("retried_by_queue_id"),
        "retried_at": _serialise_datetime(doc.get("retried_at")),
        "execution_envelope_version": doc.get("execution_envelope_version"),
        "task_concept_id": doc.get("task_concept_id"),
        "task_execution_concept_id": doc.get("task_execution_concept_id"),
        "client_request_id": doc.get("client_request_id"),
        "attempt_id": doc.get("attempt_id"),
        "conversation_key": doc.get("conversation_key"),
        "server_instance_id": doc.get("server_instance_id"),
        "lease_acquired_at": _serialise_datetime(doc.get("lease_acquired_at")),
        "lease_heartbeat_at": _serialise_datetime(doc.get("lease_heartbeat_at")),
        "lease_expires_at": _serialise_datetime(doc.get("lease_expires_at")),
        "cancellation_requested": isinstance(
            doc.get("cancellation_requested_at"), datetime
        ),
        "cancellation_requested_at": _serialise_datetime(
            doc.get("cancellation_requested_at")
        ),
        "next_dispatch_at": _serialise_datetime(doc.get("next_dispatch_at")),
        "purge_after": _serialise_datetime(doc.get("purge_after")),
        "stale_advisory": bool(doc.get("stale_advisory")),
        "stale_advisory_at": _serialise_datetime(doc.get("stale_advisory_at")),
        "stale_advisory_reason": doc.get("stale_advisory_reason"),
        "reconciliation_required": bool(doc.get("reconciliation_required")),
        "task_execution_reconciliation_status": doc.get(
            "task_execution_reconciliation_status"
        ),
        "task_execution_reconciliation_queue_status": doc.get(
            "task_execution_reconciliation_queue_status"
        ),
        "task_execution_reconciliation_pending_at": _serialise_datetime(
            doc.get("task_execution_reconciliation_pending_at")
        ),
        "task_execution_reconciled_at": _serialise_datetime(
            doc.get("task_execution_reconciled_at")
        ),
    }


def list_active_queue_records(
    *,
    scope: Mapping[str, Any],
    statuses: Sequence[str] = ACTIVE_STATUSES,
    limit: int = 100,
) -> list[dict[str, Any]]:
    allowed_statuses = [
        str(status) for status in statuses if str(status) in VALID_STATUSES
    ]
    if not allowed_statuses:
        allowed_statuses = list(ACTIVE_STATUSES)
    max_limit = max(1, min(int(limit or 100), 500))
    if STATUS_IN_PROGRESS in allowed_statuses:
        expire_stale_in_progress_records(scope=scope)
    query = {
        **_compatible_scope_query(scope),
        "status": {"$in": allowed_statuses},
    }
    docs = _collection().find(query).sort(_enqueue_sort()).limit(max_limit)
    return [
        record for record in (serialise_queue_record(doc) for doc in docs) if record
    ]


def list_recent_failed_queue_records(
    *,
    scope: Mapping[str, Any],
    limit: int = 20,
    since_seconds: int = RECENT_FAILED_VISIBILITY_SECONDS,
) -> list[dict[str, Any]]:
    """Return bounded recent failed queue records for durable user-visible state."""

    seconds = max(1, int(since_seconds or RECENT_FAILED_VISIBILITY_SECONDS))
    cutoff = _now() - timedelta(seconds=seconds)
    max_limit = max(1, min(int(limit or 20), 100))
    query = {
        **_compatible_scope_query(scope),
        "status": STATUS_FAILED,
        "completed_at": {"$gte": cutoff},
        "$or": [
            {"retried_by_queue_id": {"$exists": False}},
            {"retried_by_queue_id": None},
            {"retried_by_queue_id": ""},
        ],
    }
    docs = list(
        _collection()
        .find(query)
        .sort([("completed_at", -1), ("updated_at", -1)])
        .limit(100)
    )
    failed_ids = [
        str(doc.get("queue_id") or "")
        for doc in docs
        if doc.get("queue_id")
    ]
    retried_source_ids: set[str] = set()
    if failed_ids:
        successors = _collection().find(
            {
                **_compatible_scope_query(scope),
                "retry_source_queue_id": {"$in": failed_ids},
            },
            {"retry_source_queue_id": 1},
        )
        retried_source_ids = {
            str(doc.get("retry_source_queue_id") or "")
            for doc in successors
            if doc.get("retry_source_queue_id")
        }
    return [
        record
        for record in (
            serialise_queue_record(doc)
            for doc in docs
            if str(doc.get("queue_id") or "") not in retried_source_ids
        )
        if record
    ][:max_limit]


def get_failed_queue_record_for_retry(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
) -> dict[str, Any]:
    """Return one actor-visible failed row as canonical retry input.

    The execution envelope is intentionally returned only to trusted server
    code. It is not added to the general queue projection merely to support a
    retry action.
    """

    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    coll = _collection()
    existing_successor = coll.find_one(
        {
            **_compatible_scope_query(scope),
            "retry_source_queue_id": queue_id_clean,
        }
    )
    if isinstance(existing_successor, Mapping):
        return {
            "source": None,
            "successor": dict(existing_successor),
        }
    source = coll.find_one(
        {
            **_compatible_scope_query(scope),
            "queue_id": queue_id_clean,
            "status": STATUS_FAILED,
        }
    )
    if not isinstance(source, Mapping):
        current = coll.find_one(
            {
                **_compatible_scope_query(scope),
                "queue_id": queue_id_clean,
            },
            {"status": 1, "retried_by_queue_id": 1},
        )
        details = {
            "current_status": current.get("status") if current else None,
            "retried_by_queue_id": (
                current.get("retried_by_queue_id") if current else None
            ),
        }
        raise ChatPromptQueueRecordNotFound(
            "The failed queue record is not available for retry",
            error_code="queue_retry_source_not_found",
            details=details,
        )
    return {"source": dict(source), "successor": None}


def mark_failed_queue_record_retried(
    *,
    scope: Mapping[str, Any],
    source_queue_id: str,
    successor_queue_id: str,
) -> dict[str, Any]:
    """Back-link a failed source after its unique successor is durable.

    A concurrent Dismiss may already have changed the source to cancelled.
    Once the successor exists, Retry has been durably accepted, so preserve
    that terminal status while recording the lineage instead of returning a
    false failure for work that can now execute.
    """

    source_id = _coerce_scope_value(source_queue_id, field="source_queue_id")
    successor_id = _coerce_scope_value(
        successor_queue_id,
        field="successor_queue_id",
    )
    coll = _collection()
    successor = coll.find_one(
        {
            **_compatible_scope_query(scope),
            "queue_id": successor_id,
            "retry_source_queue_id": source_id,
        },
        {"queue_id": 1},
    )
    if not isinstance(successor, Mapping):
        raise ChatPromptQueueRetryConflict(
            "The retry successor does not match the failed source",
            queue_id=successor_id,
        )
    now = _now()
    source = coll.find_one_and_update(
        {
            **_compatible_scope_query(scope),
            "queue_id": source_id,
            "status": {"$in": [STATUS_FAILED, STATUS_CANCELLED]},
            "$or": [
                {"retried_by_queue_id": {"$exists": False}},
                {"retried_by_queue_id": successor_id},
            ],
        },
        {
            "$set": {
                "retried_by_queue_id": successor_id,
                "retried_at": now,
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not isinstance(source, Mapping):
        raise ChatPromptQueueRetryConflict(
            "The failed source changed while its retry was being accepted",
            queue_id=successor_id,
        )
    record = serialise_queue_record(source)
    if record is None:  # pragma: no cover - defensive
        raise ChatPromptQueueUnavailable("retry source could not be serialised")
    return record


def list_queue_visibility_records(
    *, scope: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """Return active queue records plus bounded failed records for UI visibility."""

    active = list_active_queue_records(scope=scope)
    recent_failed = list_recent_failed_queue_records(scope=scope)
    # The two reads are intentionally bounded and inexpensive, but a record can
    # become failed between them. Prefer the terminal observation and never make
    # one canonical queue row appear twice in the UI projection.
    failed_queue_ids = {
        str(record.get("queue_id") or "")
        for record in recent_failed
        if record.get("queue_id")
    }
    return {
        "items": [
            record
            for record in active
            if str(record.get("queue_id") or "") not in failed_queue_ids
        ],
        "recent_failed_items": recent_failed,
    }


def backfill_legacy_terminal_purge_after(
    *,
    now: datetime | None = None,
    batch_size: int = 500,
    rollout_grace_seconds: int | None = None,
) -> int:
    """Add bounded TTL dates to legacy terminal rows without surprise expiry."""

    observed_now = now or _now()
    if observed_now.tzinfo is None:
        observed_now = observed_now.replace(tzinfo=timezone.utc)
    else:
        observed_now = observed_now.astimezone(timezone.utc)
    try:
        bounded_batch_size = max(1, min(int(batch_size), 500))
    except (TypeError, ValueError) as exc:
        raise InvalidChatPromptQueueInput("batch_size must be an integer") from exc
    rollout_floor = observed_now + timedelta(
        seconds=_terminal_backfill_grace_seconds(rollout_grace_seconds)
    )
    retention_seconds = _terminal_retention_seconds()
    coll = _collection()
    docs = (
        coll.find(
            {
                "status": {"$in": list(TERMINAL_STATUSES)},
                "purge_after": {"$exists": False},
                "$and": [
                    {
                        "$or": [
                            {"task_concept_id": {"$exists": False}},
                            {"task_concept_id": ""},
                            {"task_concept_id": None},
                        ]
                    },
                    {
                        "$or": [
                            {"task_execution_concept_id": {"$exists": False}},
                            {"task_execution_concept_id": ""},
                            {"task_execution_concept_id": None},
                        ]
                    },
                ],
            },
            {"_id": 1, "queue_id": 1, "status": 1, "completed_at": 1, "updated_at": 1},
        )
        .sort([("completed_at", 1), ("updated_at", 1), ("queue_id", 1)])
        .limit(bounded_batch_size)
    )
    modified = 0
    for doc in docs:
        terminal_at = doc.get("completed_at") or doc.get("updated_at") or observed_now
        if not isinstance(terminal_at, datetime):
            terminal_at = observed_now
        elif terminal_at.tzinfo is None:
            terminal_at = terminal_at.replace(tzinfo=timezone.utc)
        else:
            terminal_at = terminal_at.astimezone(timezone.utc)
        purge_after = max(
            terminal_at + timedelta(seconds=retention_seconds),
            rollout_floor,
        )
        identity_query: dict[str, Any]
        if doc.get("_id") is not None:
            identity_query = {"_id": doc["_id"]}
        else:  # pragma: no cover - Mongo documents always have _id
            identity_query = {"queue_id": doc.get("queue_id")}
        result = coll.update_one(
            {
                **identity_query,
                "status": {"$in": list(TERMINAL_STATUSES)},
                "purge_after": {"$exists": False},
            },
            {"$set": {"purge_after": purge_after}},
        )
        modified += int(getattr(result, "modified_count", 0) or 0)
    return modified


def backfill_linked_terminal_reconciliation(
    *,
    now: datetime | None = None,
    batch_size: int = 500,
) -> int:
    """Fence legacy linked terminal rows from TTL and make them retryable."""

    observed_now = now or _now()
    try:
        bounded_batch_size = max(1, min(int(batch_size), 500))
    except (TypeError, ValueError) as exc:
        raise InvalidChatPromptQueueInput("batch_size must be an integer") from exc
    coll = _collection()
    docs = (
        coll.find(
            {
                "status": {"$in": list(TERMINAL_STATUSES)},
                "task_execution_reconciliation_status": {"$exists": False},
                "$or": [
                    {"task_concept_id": {"$exists": True, "$nin": [None, ""]}},
                    {
                        "task_execution_concept_id": {
                            "$exists": True,
                            "$nin": [None, ""],
                        }
                    },
                ],
            },
            {
                "_id": 1,
                "queue_id": 1,
                "status": 1,
                "last_error": 1,
                "completed_at": 1,
            },
        )
        .sort([("completed_at", 1), ("queue_id", 1)])
        .limit(bounded_batch_size)
    )
    modified = 0
    for doc in docs:
        pending_at = (
            doc.get("completed_at")
            if isinstance(doc.get("completed_at"), datetime)
            else observed_now
        )
        result = coll.update_one(
            {
                "_id": doc.get("_id"),
                "status": doc.get("status"),
                "task_execution_reconciliation_status": {"$exists": False},
            },
            {
                "$set": {
                    "task_execution_reconciliation_status": (
                        TASK_EXECUTION_RECONCILIATION_PENDING
                    ),
                    "task_execution_reconciliation_queue_status": doc.get("status"),
                    "task_execution_reconciliation_error": doc.get("last_error"),
                    "task_execution_reconciliation_source": (
                        "legacy_linked_terminal_backfill"
                    ),
                    "task_execution_reconciliation_pending_at": pending_at,
                    "task_execution_reconciliation_updated_at": observed_now,
                },
                "$unset": {"purge_after": ""},
            },
        )
        modified += int(getattr(result, "modified_count", 0) or 0)
    return modified


def _internal_task_execution_reconciliation_record(
    doc: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    record = serialise_queue_record(doc)
    if record is None or not isinstance(doc, Mapping):
        return None
    for field in (
        "user_concept_id",
        "organisation_concept_id",
        "namespace",
        "task_execution_reconciliation_token",
        "task_execution_reconciliation_owner",
        "task_execution_reconciliation_error",
        "task_execution_reconciliation_source",
    ):
        record[field] = doc.get(field)
    record["task_execution_reconciliation_lease_expires_at"] = _serialise_datetime(
        doc.get("task_execution_reconciliation_lease_expires_at")
    )
    return record


def claim_task_execution_terminal_reconciliation(
    *,
    server_instance_id: str,
    queue_id: str | None = None,
    now: datetime | None = None,
    lease_seconds: int | None = None,
) -> dict[str, Any] | None:
    """Claim one durable linked-terminal projection with an expiring lease."""

    owner = _coerce_scope_value(server_instance_id, field="server_instance_id")
    queue_id_clean = _coerce_scope_value(
        queue_id,
        field="queue_id",
        required=False,
    )
    observed_now = now or _now()
    query: dict[str, Any] = {
        "status": {"$in": list(TERMINAL_STATUSES)},
        "$and": [
            {
                "$or": [
                    {
                        "task_execution_reconciliation_status": (
                            TASK_EXECUTION_RECONCILIATION_PENDING
                        )
                    },
                    {
                        "task_execution_reconciliation_status": (
                            TASK_EXECUTION_RECONCILIATION_CLAIMED
                        ),
                        "task_execution_reconciliation_lease_expires_at": {
                            "$lte": observed_now
                        },
                    },
                ]
            },
            {
                "$or": [
                    {"task_execution_reconciliation_next_at": {"$exists": False}},
                    {
                        "task_execution_reconciliation_next_at": {
                            "$lte": observed_now
                        }
                    },
                ]
            },
        ],
    }
    if queue_id_clean:
        query["queue_id"] = queue_id_clean
    token = str(uuid4())
    doc = _collection().find_one_and_update(
        query,
        {
            "$set": {
                "task_execution_reconciliation_status": (
                    TASK_EXECUTION_RECONCILIATION_CLAIMED
                ),
                "task_execution_reconciliation_token": token,
                "task_execution_reconciliation_owner": owner,
                "task_execution_reconciliation_acquired_at": observed_now,
                "task_execution_reconciliation_heartbeat_at": observed_now,
                "task_execution_reconciliation_lease_expires_at": observed_now
                + timedelta(
                    seconds=_task_execution_reconciliation_lease_seconds(
                        lease_seconds
                    )
                ),
                "task_execution_reconciliation_updated_at": observed_now,
            },
            "$unset": {
                "purge_after": "",
                "task_execution_reconciliation_next_at": "",
            },
            "$inc": {"task_execution_reconciliation_attempt_count": 1},
        },
        sort=[("task_execution_reconciliation_pending_at", 1), ("queue_id", 1)],
        return_document=ReturnDocument.AFTER,
    )
    return _internal_task_execution_reconciliation_record(doc)


def release_task_execution_terminal_reconciliation(
    *,
    queue_id: str,
    reconciliation_token: str,
    server_instance_id: str,
    error: str,
    retry_after_seconds: float = 1.0,
    now: datetime | None = None,
) -> bool:
    """Return an exact failed reconciliation claim to the durable retry queue."""

    try:
        delay = max(0.0, float(retry_after_seconds))
    except (TypeError, ValueError) as exc:
        raise InvalidChatPromptQueueInput(
            "retry_after_seconds must be a non-negative number"
        ) from exc
    if not math.isfinite(delay):
        raise InvalidChatPromptQueueInput("retry_after_seconds must be finite")
    observed_now = now or _now()
    result = _collection().update_one(
        {
            "queue_id": _coerce_scope_value(queue_id, field="queue_id"),
            "task_execution_reconciliation_status": (
                TASK_EXECUTION_RECONCILIATION_CLAIMED
            ),
            "task_execution_reconciliation_token": _coerce_scope_value(
                reconciliation_token,
                field="reconciliation_token",
            ),
            "task_execution_reconciliation_owner": _coerce_scope_value(
                server_instance_id,
                field="server_instance_id",
            ),
        },
        {
            "$set": {
                "task_execution_reconciliation_status": (
                    TASK_EXECUTION_RECONCILIATION_PENDING
                ),
                "task_execution_reconciliation_next_at": observed_now
                + timedelta(seconds=delay),
                "task_execution_reconciliation_last_error": _coerce_text(
                    error,
                    field="error",
                    required=True,
                    max_chars=4_000,
                ),
                "task_execution_reconciliation_updated_at": observed_now,
            },
            "$unset": {
                "purge_after": "",
                "task_execution_reconciliation_token": "",
                "task_execution_reconciliation_owner": "",
                "task_execution_reconciliation_acquired_at": "",
                "task_execution_reconciliation_heartbeat_at": "",
                "task_execution_reconciliation_lease_expires_at": "",
            },
        },
    )
    return bool(getattr(result, "matched_count", 0))


def acknowledge_task_execution_terminal_reconciliation(
    *,
    queue_id: str,
    reconciliation_token: str,
    server_instance_id: str,
    task_execution_concept_id: str,
    enqueue_submission_id: str,
    queue_status: str,
    now: datetime | None = None,
) -> bool:
    """Acknowledge an exact successful TaskExecution projection by queue CAS."""

    if queue_status not in TERMINAL_STATUSES:
        raise InvalidChatPromptQueueInput("queue_status must be terminal")
    observed_now = now or _now()
    result = _collection().update_one(
        {
            "queue_id": _coerce_scope_value(queue_id, field="queue_id"),
            "status": queue_status,
            "task_execution_concept_id": _coerce_scope_value(
                task_execution_concept_id,
                field="task_execution_concept_id",
            ),
            "enqueue_submission_id": _coerce_scope_value(
                enqueue_submission_id,
                field="enqueue_submission_id",
            ),
            "task_execution_reconciliation_status": (
                TASK_EXECUTION_RECONCILIATION_CLAIMED
            ),
            "task_execution_reconciliation_queue_status": queue_status,
            "task_execution_reconciliation_token": _coerce_scope_value(
                reconciliation_token,
                field="reconciliation_token",
            ),
            "task_execution_reconciliation_owner": _coerce_scope_value(
                server_instance_id,
                field="server_instance_id",
            ),
        },
        {
            "$set": {
                "task_execution_reconciliation_status": (
                    TASK_EXECUTION_RECONCILIATION_ACKNOWLEDGED
                ),
                "task_execution_reconciled_at": observed_now,
                "task_execution_reconciliation_updated_at": observed_now,
                # Retention begins only once the cross-store projection is
                # durably acknowledged, never at the earlier queue terminal CAS.
                "purge_after": _terminal_purge_after(observed_now),
            },
            "$unset": {
                "task_execution_reconciliation_token": "",
                "task_execution_reconciliation_owner": "",
                "task_execution_reconciliation_acquired_at": "",
                "task_execution_reconciliation_heartbeat_at": "",
                "task_execution_reconciliation_lease_expires_at": "",
                "task_execution_reconciliation_next_at": "",
                "task_execution_reconciliation_last_error": "",
            },
        },
    )
    return bool(getattr(result, "matched_count", 0))


def _internal_task_launch_reconciliation_record(
    doc: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    record = serialise_queue_record(doc)
    if record is None or not isinstance(doc, Mapping):
        return None
    for field in (
        "user_concept_id",
        "organisation_concept_id",
        "namespace",
        "task_launch_reconciliation_token",
        "task_launch_reconciliation_owner",
        "task_launch_reconciliation_last_error",
    ):
        record[field] = doc.get(field)
    record["execution_envelope"] = (
        dict(doc.get("execution_envelope"))
        if isinstance(doc.get("execution_envelope"), Mapping)
        else {}
    )
    record["task_launch_reconciliation_lease_expires_at"] = _serialise_datetime(
        doc.get("task_launch_reconciliation_lease_expires_at")
    )
    return record


def claim_task_execution_launch_reconciliation(
    *,
    server_instance_id: str,
    now: datetime | None = None,
    lease_seconds: int | None = None,
) -> dict[str, Any] | None:
    """Claim one unready task launch outbox with an expiring server lease."""

    owner = _coerce_scope_value(server_instance_id, field="server_instance_id")
    observed_now = now or _now()
    token = str(uuid4())
    doc = _collection().find_one_and_update(
        {
            "status": STATUS_QUEUED,
            "dispatch_mode": DISPATCH_MODE_SERVER,
            "dispatch_ready": False,
            "task_concept_id": {"$exists": True, "$nin": [None, ""]},
            "task_execution_concept_id": {
                "$exists": True,
                "$nin": [None, ""],
            },
            "$and": [
                {
                    "$or": [
                        {"task_launch_reconciliation_next_at": {"$exists": False}},
                        {"task_launch_reconciliation_next_at": {"$lte": observed_now}},
                    ]
                },
                {
                    "$or": [
                        {"task_launch_reconciliation_token": {"$exists": False}},
                        {
                            "task_launch_reconciliation_lease_expires_at": {
                                "$lte": observed_now
                            }
                        },
                    ]
                },
            ],
        },
        {
            "$set": {
                "task_launch_reconciliation_token": token,
                "task_launch_reconciliation_owner": owner,
                "task_launch_reconciliation_acquired_at": observed_now,
                "task_launch_reconciliation_lease_expires_at": observed_now
                + timedelta(seconds=_dispatch_lease_seconds(lease_seconds)),
                "task_launch_reconciliation_updated_at": observed_now,
            },
            "$unset": {"task_launch_reconciliation_next_at": ""},
            "$inc": {"task_launch_reconciliation_attempt_count": 1},
        },
        sort=_enqueue_sort(),
        return_document=ReturnDocument.AFTER,
    )
    return _internal_task_launch_reconciliation_record(doc)


def release_task_execution_launch_reconciliation(
    *,
    queue_id: str,
    reconciliation_token: str,
    server_instance_id: str,
    error: str,
    retry_after_seconds: float = 1.0,
    now: datetime | None = None,
) -> bool:
    """Release an exact launch projection claim for durable retry."""

    try:
        delay = max(0.0, float(retry_after_seconds))
    except (TypeError, ValueError) as exc:
        raise InvalidChatPromptQueueInput(
            "retry_after_seconds must be a non-negative number"
        ) from exc
    if not math.isfinite(delay):
        raise InvalidChatPromptQueueInput("retry_after_seconds must be finite")
    observed_now = now or _now()
    result = _collection().update_one(
        {
            "queue_id": _coerce_scope_value(queue_id, field="queue_id"),
            "status": STATUS_QUEUED,
            "dispatch_ready": False,
            "task_launch_reconciliation_token": _coerce_scope_value(
                reconciliation_token,
                field="reconciliation_token",
            ),
            "task_launch_reconciliation_owner": _coerce_scope_value(
                server_instance_id,
                field="server_instance_id",
            ),
        },
        {
            "$set": {
                "task_launch_reconciliation_next_at": observed_now
                + timedelta(seconds=delay),
                "task_launch_reconciliation_last_error": _coerce_text(
                    error,
                    field="error",
                    required=True,
                    max_chars=4_000,
                ),
                "task_launch_reconciliation_updated_at": observed_now,
            },
            "$unset": {
                "task_launch_reconciliation_token": "",
                "task_launch_reconciliation_owner": "",
                "task_launch_reconciliation_acquired_at": "",
                "task_launch_reconciliation_lease_expires_at": "",
            },
        },
    )
    return bool(getattr(result, "matched_count", 0))


def acknowledge_task_execution_launch_reconciliation(
    *,
    queue_id: str,
    reconciliation_token: str,
    server_instance_id: str,
    task_execution_concept_id: str,
    enqueue_submission_id: str,
    now: datetime | None = None,
) -> bool:
    """Activate one exact task launch after its TaskExecution is read back."""

    observed_now = now or _now()
    result = _collection().update_one(
        {
            "queue_id": _coerce_scope_value(queue_id, field="queue_id"),
            "status": STATUS_QUEUED,
            "dispatch_ready": False,
            "task_execution_concept_id": _coerce_scope_value(
                task_execution_concept_id,
                field="task_execution_concept_id",
            ),
            "enqueue_submission_id": _coerce_scope_value(
                enqueue_submission_id,
                field="enqueue_submission_id",
            ),
            "task_launch_reconciliation_token": _coerce_scope_value(
                reconciliation_token,
                field="reconciliation_token",
            ),
            "task_launch_reconciliation_owner": _coerce_scope_value(
                server_instance_id,
                field="server_instance_id",
            ),
        },
        {
            "$set": {
                "dispatch_ready": True,
                "dispatch_ready_at": observed_now,
                "task_launch_reconciled_at": observed_now,
                "updated_at": observed_now,
            },
            "$unset": {
                "task_launch_reconciliation_token": "",
                "task_launch_reconciliation_owner": "",
                "task_launch_reconciliation_acquired_at": "",
                "task_launch_reconciliation_lease_expires_at": "",
                "task_launch_reconciliation_next_at": "",
                "task_launch_reconciliation_last_error": "",
            },
        },
    )
    return bool(getattr(result, "matched_count", 0))


def fail_task_execution_launch_reconciliation(
    *,
    queue_id: str,
    reconciliation_token: str,
    server_instance_id: str,
    error: str,
    now: datetime | None = None,
) -> bool:
    """Terminalise a permanently invalid task launch without invoking Von."""

    observed_now = now or _now()
    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    token_clean = _coerce_scope_value(
        reconciliation_token,
        field="reconciliation_token",
    )
    owner_clean = _coerce_scope_value(
        server_instance_id,
        field="server_instance_id",
    )
    error_clean = _coerce_text(
        error,
        field="error",
        required=True,
        max_chars=4_000,
    )
    coll = _collection()
    exact_query = {
        "queue_id": queue_id_clean,
        "status": STATUS_QUEUED,
        "dispatch_ready": False,
        "task_launch_reconciliation_token": token_clean,
        "task_launch_reconciliation_owner": owner_clean,
    }
    existing = coll.find_one(exact_query)
    reconciliation_set, reconciliation_unset = _terminal_reconciliation_update(
        existing,
        status=STATUS_FAILED,
        error=error_clean,
        source="task_launch_reconciliation",
        observed_now=observed_now,
    )
    result = coll.update_one(
        exact_query,
        {
            "$set": {
                "status": STATUS_FAILED,
                "completed_at": observed_now,
                "updated_at": observed_now,
                "last_error": error_clean,
                **reconciliation_set,
            },
            "$unset": {
                "active_task_execution_key": "",
                "queued_global_slot": "",
                "queued_user_slot": "",
                "task_launch_reconciliation_token": "",
                "task_launch_reconciliation_owner": "",
                "task_launch_reconciliation_acquired_at": "",
                "task_launch_reconciliation_lease_expires_at": "",
                "task_launch_reconciliation_next_at": "",
                "task_launch_reconciliation_last_error": "",
                **reconciliation_unset,
            },
        },
    )
    return bool(getattr(result, "matched_count", 0))


def create_queue_record(
    *,
    scope: Mapping[str, Any],
    prompt_raw: Any,
    session_id: Any = None,
    session_name: Any = None,
    status: str = STATUS_QUEUED,
    source: str = "queued",
    client_request_id: Any = None,
    attempt_id: Any = None,
    conversation_key: Any = None,
    legacy_submission_role: str | None = None,
    window_session_id: Any = None,
    enqueue_submission_id: Any = None,
    dispatch_mode: Any = None,
    execution_envelope: Any = None,
    execution_envelope_version: Any = None,
    task_concept_id: Any = None,
    task_execution_concept_id: Any = None,
    server_dispatch_ready: Any = None,
    handoff_source_queue_id: Any = None,
    retry_source_queue_id: Any = None,
    retry_request_id: Any = None,
    retry_source_task_execution_concept_id: Any = None,
) -> dict[str, Any]:
    if status not in {STATUS_QUEUED, STATUS_IN_PROGRESS}:
        raise InvalidChatPromptQueueInput("status must be queued or in_progress")
    prompt = _coerce_text(
        prompt_raw,
        field="prompt_raw",
        required=True,
        max_chars=MAX_PROMPT_RAW_CHARS,
    )
    session_id_clean = _coerce_scope_value(
        session_id, field="session_id", required=False
    )
    session_name_clean = _coerce_text(
        session_name,
        field="session_name",
        required=False,
        max_chars=MAX_SESSION_NAME_CHARS,
    )
    if session_name_clean is not None:
        session_name_clean = session_name_clean.strip() or None
    client_request_id_clean = _coerce_scope_value(
        client_request_id,
        field="client_request_id",
        required=False,
    )
    attempt_id_clean = _coerce_scope_value(
        attempt_id,
        field="attempt_id",
        required=False,
    )
    conversation_key_clean = _coerce_scope_value(
        conversation_key,
        field="conversation_key",
        required=False,
    )
    enqueue_submission_id_clean = _coerce_scope_value(
        enqueue_submission_id,
        field="enqueue_submission_id",
        required=False,
    )
    dispatch_mode_clean = _coerce_scope_value(
        dispatch_mode,
        field="dispatch_mode",
        required=False,
    )
    task_concept_id_clean = _coerce_scope_value(
        task_concept_id,
        field="task_concept_id",
        required=False,
    )
    task_execution_concept_id_clean = _coerce_scope_value(
        task_execution_concept_id,
        field="task_execution_concept_id",
        required=False,
    )
    handoff_source_queue_id_clean = _coerce_scope_value(
        handoff_source_queue_id,
        field="handoff_source_queue_id",
        required=False,
    )
    retry_source_queue_id_clean = _coerce_scope_value(
        retry_source_queue_id,
        field="retry_source_queue_id",
        required=False,
    )
    retry_request_id_clean = _coerce_scope_value(
        retry_request_id,
        field="retry_request_id",
        required=False,
    )
    retry_source_task_execution_concept_id_clean = _coerce_scope_value(
        retry_source_task_execution_concept_id,
        field="retry_source_task_execution_concept_id",
        required=False,
    )
    if bool(task_concept_id_clean) != bool(task_execution_concept_id_clean):
        raise InvalidChatPromptQueueInput(
            "task_concept_id and task_execution_concept_id must be supplied together"
        )
    if task_concept_id_clean and dispatch_mode_clean != DISPATCH_MODE_SERVER:
        raise InvalidChatPromptQueueInput(
            "task-linked queue records require server dispatch"
        )
    if handoff_source_queue_id_clean and dispatch_mode_clean != DISPATCH_MODE_SERVER:
        raise InvalidChatPromptQueueInput(
            "handoff_source_queue_id requires server dispatch"
        )
    if handoff_source_queue_id_clean and task_concept_id_clean:
        raise InvalidChatPromptQueueInput(
            "handoff_source_queue_id cannot be combined with task execution links"
        )
    if handoff_source_queue_id_clean and retry_source_queue_id_clean:
        raise InvalidChatPromptQueueInput(
            "handoff and retry source links cannot be combined"
        )
    if retry_source_queue_id_clean and dispatch_mode_clean != DISPATCH_MODE_SERVER:
        raise InvalidChatPromptQueueInput(
            "retry_source_queue_id requires server dispatch"
        )
    if bool(retry_source_queue_id_clean) != bool(retry_request_id_clean):
        raise InvalidChatPromptQueueInput(
            "retry_source_queue_id and retry_request_id must be supplied together"
        )
    if (
        retry_source_task_execution_concept_id_clean
        and not retry_source_queue_id_clean
    ):
        raise InvalidChatPromptQueueInput(
            "retry task lineage requires retry_source_queue_id"
        )
    active_task_execution_key = (
        _active_task_execution_key(task_concept_id_clean)
        if task_concept_id_clean
        else None
    )
    envelope: dict[str, Any] | None = None
    envelope_version: int | None = None
    enqueue_fingerprint: str | None = None
    dispatch_ready = True
    if dispatch_mode_clean is not None and dispatch_mode_clean != DISPATCH_MODE_SERVER:
        raise InvalidChatPromptQueueInput("dispatch_mode must be server")
    if dispatch_mode_clean == DISPATCH_MODE_SERVER:
        if status != STATUS_QUEUED:
            raise InvalidChatPromptQueueInput(
                "server-dispatched queue records must start queued"
            )
        if not enqueue_submission_id_clean:
            raise InvalidChatPromptQueueInput(
                "enqueue_submission_id is required for server dispatch"
            )
        if not conversation_key_clean:
            raise InvalidChatPromptQueueInput(
                "conversation_key is required for server dispatch"
            )
        if client_request_id_clean or attempt_id_clean:
            raise InvalidChatPromptQueueInput(
                "server dispatch assigns request and attempt identifiers at reservation"
            )
        if execution_envelope_version is None:
            envelope_version = EXECUTION_ENVELOPE_VERSION
        elif (
            isinstance(execution_envelope_version, bool)
            or not isinstance(execution_envelope_version, int)
            or execution_envelope_version != EXECUTION_ENVELOPE_VERSION
        ):
            raise InvalidChatPromptQueueInput(
                f"execution_envelope_version must be {EXECUTION_ENVELOPE_VERSION}"
            )
        else:
            envelope_version = EXECUTION_ENVELOPE_VERSION
        envelope = _normalise_execution_envelope(execution_envelope)
        if server_dispatch_ready is not None:
            if not isinstance(server_dispatch_ready, bool):
                raise InvalidChatPromptQueueInput(
                    "server_dispatch_ready must be a boolean"
                )
            dispatch_ready = server_dispatch_ready
        if handoff_source_queue_id_clean:
            if server_dispatch_ready is True:
                raise InvalidChatPromptQueueInput(
                    "a handoff replacement must remain dispatch-ineligible until its source is retired"
                )
            dispatch_ready = False
        enqueue_fingerprint = _enqueue_fingerprint(
            scope=scope,
            prompt_raw=str(prompt),
            session_id=session_id_clean,
            session_name=session_name_clean,
            conversation_key=conversation_key_clean,
            execution_envelope=envelope,
            execution_envelope_version=envelope_version,
            task_concept_id=task_concept_id_clean,
            task_execution_concept_id=task_execution_concept_id_clean,
            dispatch_ready=dispatch_ready,
            handoff_source_queue_id=handoff_source_queue_id_clean,
            retry_source_queue_id=retry_source_queue_id_clean,
        )
    elif any(
        value is not None
        for value in (
            enqueue_submission_id_clean,
            execution_envelope,
            execution_envelope_version,
            server_dispatch_ready,
            handoff_source_queue_id_clean,
            retry_source_queue_id_clean,
            retry_request_id_clean,
            retry_source_task_execution_concept_id_clean,
        )
    ):
        raise InvalidChatPromptQueueInput(
            "enqueue submission and execution envelope require server dispatch"
        )
    legacy_role_clean = _coerce_scope_value(
        legacy_submission_role,
        field="legacy_submission_role",
        required=False,
    )
    if legacy_role_clean and legacy_role_clean not in LEGACY_SUBMISSION_ROLES:
        raise InvalidChatPromptQueueInput(
            "legacy_submission_role must be queue or generate"
        )
    active_legacy_submission_key = (
        _build_active_legacy_submission_key(
            scope=scope,
            conversation_key=conversation_key_clean,
            prompt_raw=prompt,
            window_session_id=window_session_id,
        )
        if legacy_role_clean
        else None
    )
    now = _now()
    doc = {
        "queue_id": str(uuid4()),
        **_scope_query(scope),
        "prompt_raw": prompt,
        "status": status,
        "session_id": session_id_clean,
        "session_name": session_name_clean,
        "source": source,
        "client_request_id": client_request_id_clean,
        "attempt_id": attempt_id_clean,
        "conversation_key": conversation_key_clean,
        "attempt_count": 1 if status == STATUS_IN_PROGRESS else 0,
        "created_at": now,
        "updated_at": now,
        "queued_at": now if status == STATUS_QUEUED else None,
        "claimed_at": now if status == STATUS_IN_PROGRESS else None,
        "completed_at": None,
        "last_error": None,
    }
    if dispatch_mode_clean == DISPATCH_MODE_SERVER:
        doc.update(
            {
                "enqueue_submission_id": enqueue_submission_id_clean,
                "enqueue_fingerprint": enqueue_fingerprint,
                "dispatch_mode": DISPATCH_MODE_SERVER,
                "execution_envelope": envelope,
                "execution_envelope_version": envelope_version,
                "dispatch_ready": dispatch_ready,
                **(
                    {"handoff_source_queue_id": handoff_source_queue_id_clean}
                    if handoff_source_queue_id_clean
                    else {}
                ),
                **(
                    {
                        "retry_source_queue_id": retry_source_queue_id_clean,
                        "retry_request_id": retry_request_id_clean,
                        **(
                            {
                                "retry_source_task_execution_concept_id": (
                                    retry_source_task_execution_concept_id_clean
                                )
                            }
                            if retry_source_task_execution_concept_id_clean
                            else {}
                        ),
                    }
                    if retry_source_queue_id_clean
                    else {}
                ),
            }
        )
    if task_concept_id_clean:
        doc["task_concept_id"] = task_concept_id_clean
    if task_execution_concept_id_clean:
        doc["task_execution_concept_id"] = task_execution_concept_id_clean
    if active_task_execution_key:
        doc["active_task_execution_key"] = active_task_execution_key
    if active_legacy_submission_key:
        doc["active_legacy_submission_key"] = active_legacy_submission_key
        doc["legacy_submission_queue_seen"] = (
            legacy_role_clean == LEGACY_SUBMISSION_ROLE_QUEUE
        )
        doc["legacy_submission_generate_seen"] = (
            legacy_role_clean == LEGACY_SUBMISSION_ROLE_GENERATE
        )
        doc["legacy_submission_expires_at"] = now + timedelta(
            seconds=LEGACY_SUBMISSION_RENDEZVOUS_SECONDS
        )
    coll = _collection()
    retry_successor = _find_retry_successor(
        coll=coll,
        scope=scope,
        retry_source_queue_id=retry_source_queue_id_clean,
    )
    if retry_successor is not None:
        return retry_successor
    if enqueue_submission_id_clean and enqueue_fingerprint:
        reusable = _find_idempotent_enqueue(
            coll=coll,
            scope=scope,
            enqueue_submission_id=enqueue_submission_id_clean,
            enqueue_fingerprint=enqueue_fingerprint,
        )
        if reusable is not None:
            return reusable
    _raise_if_handoff_source_already_bound(
        coll=coll,
        handoff_source_queue_id=handoff_source_queue_id_clean,
    )
    if active_legacy_submission_key:
        _expire_unpaired_legacy_submissions(coll=coll, now=now)
        reusable = _find_or_join_legacy_submission(
            coll=coll,
            scope=scope,
            active_legacy_submission_key=active_legacy_submission_key,
            role=str(legacy_role_clean),
            client_request_id=client_request_id_clean,
            now=now,
        )
        if reusable is not None:
            return reusable
    doc["enqueue_sequence"] = _next_enqueue_sequence()
    if status == STATUS_QUEUED:
        global_limit, per_user_limit = _foreground_queue_limits()
        user_id = str(doc["user_concept_id"])
        with _QUEUE_ADMISSION_LOCK:
            legacy_global = coll.count_documents(
                {
                    "status": STATUS_QUEUED,
                    "queued_global_slot": {"$exists": False},
                }
            )
            legacy_user = coll.count_documents(
                {
                    "status": STATUS_QUEUED,
                    "user_concept_id": user_id,
                    "queued_user_slot": {"$exists": False},
                }
            )
            available_global = max(0, global_limit - int(legacy_global))
            available_user = max(0, per_user_limit - int(legacy_user))
            occupied_global_slots = set(
                coll.distinct("queued_global_slot", {"status": STATUS_QUEUED})
            )
            occupied_user_slots = set(
                coll.distinct(
                    "queued_user_slot",
                    {"status": STATUS_QUEUED, "user_concept_id": user_id},
                )
            )
            inserted = False
            for user_slot in range(available_user):
                user_slot_key = _queued_user_slot_key(user_id, user_slot)
                if user_slot_key in occupied_user_slots:
                    continue
                for global_slot in range(available_global):
                    if global_slot in occupied_global_slots:
                        continue
                    candidate = {
                        **doc,
                        "queued_global_slot": global_slot,
                        "queued_user_slot": user_slot_key,
                    }
                    try:
                        coll.insert_one(candidate)
                    except DuplicateKeyError:
                        if enqueue_submission_id_clean and enqueue_fingerprint:
                            reusable = _find_idempotent_enqueue(
                                coll=coll,
                                scope=scope,
                                enqueue_submission_id=enqueue_submission_id_clean,
                                enqueue_fingerprint=enqueue_fingerprint,
                            )
                            if reusable is not None:
                                return reusable
                        _raise_if_handoff_source_already_bound(
                            coll=coll,
                            handoff_source_queue_id=handoff_source_queue_id_clean,
                        )
                        retry_successor = _find_retry_successor(
                            coll=coll,
                            scope=scope,
                            retry_source_queue_id=retry_source_queue_id_clean,
                        )
                        if retry_successor is not None:
                            return retry_successor
                        _raise_if_task_execution_already_active(
                            coll=coll,
                            active_task_execution_key=active_task_execution_key,
                        )
                        if active_legacy_submission_key:
                            reusable = _find_or_join_legacy_submission(
                                coll=coll,
                                scope=scope,
                                active_legacy_submission_key=active_legacy_submission_key,
                                role=str(legacy_role_clean),
                                client_request_id=client_request_id_clean,
                                now=now,
                            )
                            if reusable is not None:
                                return reusable
                        continue
                    doc = candidate
                    inserted = True
                    break
                if inserted:
                    break
            if not inserted:
                user_queued = coll.count_documents(
                    {"status": STATUS_QUEUED, "user_concept_id": user_id}
                )
                if user_queued >= per_user_limit:
                    raise ChatPromptQueueCapacityReached(
                        "This user has reached the queued-turn backlog limit",
                        limit_kind="user",
                    )
                raise ChatPromptQueueCapacityReached(
                    "Von has reached the global queued-turn backlog limit",
                    limit_kind="global",
                )
    else:
        try:
            coll.insert_one(doc)
        except DuplicateKeyError:
            if enqueue_submission_id_clean and enqueue_fingerprint:
                reusable = _find_idempotent_enqueue(
                    coll=coll,
                    scope=scope,
                    enqueue_submission_id=enqueue_submission_id_clean,
                    enqueue_fingerprint=enqueue_fingerprint,
                )
                if reusable is not None:
                    return reusable
            _raise_if_handoff_source_already_bound(
                coll=coll,
                handoff_source_queue_id=handoff_source_queue_id_clean,
            )
            retry_successor = _find_retry_successor(
                coll=coll,
                scope=scope,
                retry_source_queue_id=retry_source_queue_id_clean,
            )
            if retry_successor is not None:
                return retry_successor
            _raise_if_task_execution_already_active(
                coll=coll,
                active_task_execution_key=active_task_execution_key,
            )
            if active_legacy_submission_key:
                reusable = _find_or_join_legacy_submission(
                    coll=coll,
                    scope=scope,
                    active_legacy_submission_key=active_legacy_submission_key,
                    role=str(legacy_role_clean),
                    client_request_id=client_request_id_clean,
                    now=now,
                )
                if reusable is not None:
                    return reusable
            raise
    record = (
        _idempotent_enqueue_result(doc, replayed=False)
        if enqueue_submission_id_clean
        else serialise_queue_record(doc)
    )
    if record is None:  # pragma: no cover - defensive
        raise ChatPromptQueueUnavailable("created queue record could not be serialised")
    return record


def activate_server_dispatch_record(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
    enqueue_submission_id: str,
    task_execution_concept_id: str,
) -> dict[str, Any]:
    """Make one exactly linked task queue row eligible for server dispatch."""

    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    enqueue_id = _coerce_scope_value(
        enqueue_submission_id,
        field="enqueue_submission_id",
    )
    execution_id = _coerce_scope_value(
        task_execution_concept_id,
        field="task_execution_concept_id",
    )
    now = _now()
    coll = _collection()
    exact_query = {
        **_compatible_scope_query(scope),
        "queue_id": queue_id_clean,
        "status": STATUS_QUEUED,
        "dispatch_mode": DISPATCH_MODE_SERVER,
        "enqueue_submission_id": enqueue_id,
        "task_execution_concept_id": execution_id,
    }
    doc = coll.find_one_and_update(
        {
            **exact_query,
            "dispatch_ready": False,
            "task_launch_reconciliation_token": {"$exists": False},
        },
        {
            "$set": {
                "dispatch_ready": True,
                "dispatch_ready_at": now,
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        doc = coll.find_one({**exact_query, "dispatch_ready": True})
    record = serialise_queue_record(doc)
    if record is None:
        raise ChatPromptQueueRecordNotFound(
            "The linked server-dispatch row could not be activated",
            error_code="server_dispatch_activation_mismatch",
        )
    return record


def complete_server_dispatch_handoff(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
    enqueue_submission_id: str | None = None,
) -> dict[str, Any]:
    """Retire one exact legacy source before activating its server replacement.

    The replacement is inserted with ``dispatch_ready=False`` and carries the
    durable source identifier.  That makes the two-document handoff safely
    retryable: a crash can leave work waiting, but cannot make both rows
    dispatchable.
    """

    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    enqueue_id = _coerce_scope_value(
        enqueue_submission_id,
        field="enqueue_submission_id",
        required=False,
    )
    coll = _collection()
    replacement_query: dict[str, Any] = {
        **_compatible_scope_query(scope),
        "queue_id": queue_id_clean,
        "status": STATUS_QUEUED,
        "dispatch_mode": DISPATCH_MODE_SERVER,
        "handoff_source_queue_id": {"$exists": True, "$ne": ""},
    }
    if enqueue_id:
        replacement_query["enqueue_submission_id"] = enqueue_id
    replacement = coll.find_one(replacement_query)
    if not isinstance(replacement, Mapping):
        raise ChatPromptQueueRecordNotFound(
            "The server-dispatch handoff replacement was not found",
            error_code="queue_handoff_replacement_not_found",
        )

    source_queue_id = _coerce_scope_value(
        replacement.get("handoff_source_queue_id"),
        field="handoff_source_queue_id",
    )
    if source_queue_id == queue_id_clean:
        raise ChatPromptQueueHandoffConflict(
            "A queue handoff cannot replace itself"
        )
    source = coll.find_one(
        {
            **_compatible_scope_query(scope),
            "queue_id": source_queue_id,
        }
    )
    if not isinstance(source, Mapping):
        cancel_prompt_record(scope=scope, queue_id=queue_id_clean)
        raise ChatPromptQueueHandoffConflict(
            "The handoff source is unavailable for canonical retirement"
        )
    if source.get("dispatch_mode") == DISPATCH_MODE_SERVER:
        cancel_prompt_record(scope=scope, queue_id=queue_id_clean)
        raise ChatPromptQueueHandoffConflict(
            "A server-dispatch row cannot be used as a legacy handoff source"
        )
    for field in ("prompt_raw", "session_id", "conversation_key"):
        if source.get(field) != replacement.get(field):
            cancel_prompt_record(scope=scope, queue_id=queue_id_clean)
            raise ChatPromptQueueHandoffConflict(
                f"The handoff source does not match replacement field {field}"
            )

    try:
        cancel_prompt_record(
            scope=scope,
            queue_id=source_queue_id,
            handoff_replacement_queue_id=queue_id_clean,
        )
    except ConversationTurnAlreadyActive:
        # A live source may become cancellable later. Leave the replacement
        # dispatch-ineligible so the scheduled reconciler can retry.
        raise
    except ChatPromptQueueHandoffConflict:
        cancel_prompt_record(scope=scope, queue_id=queue_id_clean)
        raise
    except ChatPromptQueueRecordNotFound as exc:
        cancel_prompt_record(scope=scope, queue_id=queue_id_clean)
        raise ChatPromptQueueHandoffConflict(
            "The handoff source already reached a non-cancellable terminal state",
            queue_id=queue_id_clean,
        ) from exc

    now = _now()
    exact_replacement_query = {
        **_compatible_scope_query(scope),
        "queue_id": queue_id_clean,
        "status": STATUS_QUEUED,
        "dispatch_mode": DISPATCH_MODE_SERVER,
        "handoff_source_queue_id": source_queue_id,
    }
    if enqueue_id:
        exact_replacement_query["enqueue_submission_id"] = enqueue_id
    activated = coll.find_one_and_update(
        {**exact_replacement_query, "dispatch_ready": False},
        {
            "$set": {
                "dispatch_ready": True,
                "handoff_completed_at": now,
                "updated_at": now,
            },
            "$unset": {
                "handoff_reconciliation_next_at": "",
                "handoff_reconciliation_last_error": "",
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if activated is None:
        activated = coll.find_one({**exact_replacement_query, "dispatch_ready": True})
    record = serialise_queue_record(activated)
    if record is None:
        raise ChatPromptQueueRecordNotFound(
            "The server-dispatch handoff could not be activated",
            error_code="queue_handoff_activation_mismatch",
        )
    return record


def reconcile_next_server_dispatch_handoff(
    *,
    now: datetime | None = None,
    retry_after_seconds: int = DEFAULT_HANDOFF_RECONCILIATION_RETRY_SECONDS,
) -> dict[str, Any] | None:
    """Claim and complete at most one due durable legacy handoff.

    Claiming advances ``handoff_reconciliation_next_at`` before touching the
    source.  A crash therefore delays retry only by the bounded claim window,
    while a temporarily active source cannot be selected in a tight loop or
    starve unrelated dispatch-ready work.
    """

    observed_now = now or _now()
    try:
        bounded_retry_seconds = max(1, int(retry_after_seconds))
    except (TypeError, ValueError) as exc:
        raise InvalidChatPromptQueueInput(
            "retry_after_seconds must be an integer"
        ) from exc
    next_attempt_at = observed_now + timedelta(seconds=bounded_retry_seconds)
    coll = _collection()
    pending = coll.find_one_and_update(
        {
            "status": STATUS_QUEUED,
            "dispatch_mode": DISPATCH_MODE_SERVER,
            "dispatch_ready": False,
            "handoff_source_queue_id": {"$exists": True, "$ne": ""},
            "$or": [
                {"handoff_reconciliation_next_at": {"$exists": False}},
                {"handoff_reconciliation_next_at": None},
                {"handoff_reconciliation_next_at": {"$lte": observed_now}},
            ],
        },
        {
            "$set": {
                "handoff_reconciliation_attempted_at": observed_now,
                "handoff_reconciliation_next_at": next_attempt_at,
                "updated_at": observed_now,
            },
            "$inc": {"handoff_reconciliation_attempt_count": 1},
            "$unset": {"handoff_reconciliation_last_error": ""},
        },
        sort=_enqueue_sort(),
        return_document=ReturnDocument.AFTER,
    )
    if not isinstance(pending, Mapping):
        return None
    scope = {
        "user_concept_id": pending.get("user_concept_id"),
        "organisation_concept_id": pending.get("organisation_concept_id"),
        "namespace": pending.get("namespace"),
    }
    queue_id = str(pending.get("queue_id") or "")
    try:
        return complete_server_dispatch_handoff(
            scope=scope,
            queue_id=queue_id,
            enqueue_submission_id=(
                str(pending.get("enqueue_submission_id") or "") or None
            ),
        )
    except Exception as exc:
        # Preserve a bounded, inspectable retry schedule only while the exact
        # replacement remains pending. Deterministic conflicts may already
        # have cancelled it in ``complete_server_dispatch_handoff``.
        coll.update_one(
            {
                "queue_id": queue_id,
                "status": STATUS_QUEUED,
                "dispatch_mode": DISPATCH_MODE_SERVER,
                "dispatch_ready": False,
            },
            {
                "$set": {
                    "handoff_reconciliation_next_at": next_attempt_at,
                    "handoff_reconciliation_last_error": (
                        f"{type(exc).__name__}: {exc}"
                    )[:4_000],
                    "updated_at": observed_now,
                }
            },
        )
        raise


def reserve_next_server_dispatch(
    *,
    server_instance_id: str,
    now: datetime | None = None,
    lease_seconds: int | None = None,
    scan_limit: int = 100,
) -> dict[str, Any] | None:
    """Reserve the oldest eligible server-owned FIFO head.

    The reservation uses the same unique conversation fence as an admitted
    turn. ``bind_queue_record_to_turn`` can upgrade only this opaque token, so
    there is no pre-admission window in which a second turn can enter the same
    conversation.
    """

    owner = _coerce_scope_value(
        server_instance_id,
        field="server_instance_id",
    )
    observed_now = now or _now()
    lease_duration = _dispatch_lease_seconds(lease_seconds)
    try:
        bounded_scan_limit = max(1, min(int(scan_limit), 500))
    except (TypeError, ValueError) as exc:
        raise InvalidChatPromptQueueInput("scan_limit must be an integer") from exc
    coll = _collection()
    eligible_query = {
                "status": STATUS_QUEUED,
                "dispatch_mode": DISPATCH_MODE_SERVER,
                "execution_envelope_version": EXECUTION_ENVELOPE_VERSION,
                "enqueue_submission_id": {"$exists": True, "$ne": ""},
                "conversation_key": {"$exists": True, "$ne": ""},
                "dispatch_ready": {"$ne": False},
                "$and": [
                    {
                        "$or": [
                            {"next_dispatch_at": {"$exists": False}},
                            {"next_dispatch_at": {"$lte": observed_now}},
                        ]
                    },
                    {
                        "$or": [
                            {"dispatch_reservation_token": {"$exists": False}},
                            {"dispatch_lease_expires_at": {"$lte": observed_now}},
                        ]
                    },
                ],
            }
    # Select one eligible head per conversation before applying the scan cap.
    # Otherwise a long blocked FIFO for one conversation can occupy the whole
    # candidate window and starve unrelated conversations behind it.
    candidates = coll.aggregate(
        [
            {"$match": eligible_query},
            {
                "$sort": {
                    "enqueue_sequence": 1,
                    "created_at": 1,
                    "queue_id": 1,
                }
            },
            {
                "$group": {
                    "_id": "$conversation_key",
                    "candidate": {"$first": "$$ROOT"},
                }
            },
            {"$replaceRoot": {"newRoot": "$candidate"}},
            {
                "$sort": {
                    "enqueue_sequence": 1,
                    "created_at": 1,
                    "queue_id": 1,
                }
            },
            {"$limit": bounded_scan_limit},
        ]
    )
    for candidate in candidates:
        queue_id = str(candidate.get("queue_id") or "")
        conversation_key = str(candidate.get("conversation_key") or "")
        if not queue_id or not conversation_key:
            continue
        earlier = coll.find_one(
            {
                "conversation_key": conversation_key,
                "status": {"$in": ACTIVE_STATUSES},
                "queue_id": {"$ne": queue_id},
                "$and": [_earlier_enqueue_filter(candidate)],
            },
            {"_id": 1},
        )
        if earlier is not None:
            continue

        previous_token = candidate.get("dispatch_reservation_token")
        if previous_token:
            reservation_guard: dict[str, Any] = {
                "dispatch_reservation_token": previous_token,
                "dispatch_lease_expires_at": {"$lte": observed_now},
                "active_conversation_key": conversation_key,
            }
        else:
            reservation_guard = {
                "dispatch_reservation_token": {"$exists": False},
                "active_conversation_key": {"$exists": False},
            }
        token = str(uuid4())
        request_id = str(uuid4())
        attempt_id = str(uuid4())
        try:
            reserved = coll.find_one_and_update(
                {
                    "queue_id": queue_id,
                    "status": STATUS_QUEUED,
                    "dispatch_mode": DISPATCH_MODE_SERVER,
                    "execution_envelope_version": EXECUTION_ENVELOPE_VERSION,
                    "$or": [
                        {"next_dispatch_at": {"$exists": False}},
                        {"next_dispatch_at": {"$lte": observed_now}},
                    ],
                    **reservation_guard,
                },
                {
                    "$set": {
                        "active_conversation_key": conversation_key,
                        "dispatch_reservation_token": token,
                        "dispatch_owner": owner,
                        "dispatch_acquired_at": observed_now,
                        "dispatch_heartbeat_at": observed_now,
                        "dispatch_lease_expires_at": observed_now
                        + timedelta(seconds=lease_duration),
                        "client_request_id": request_id,
                        "attempt_id": attempt_id,
                        "updated_at": observed_now,
                    },
                    "$unset": {
                        "next_dispatch_at": "",
                        "last_dispatch_error": "",
                    },
                    "$inc": {"dispatch_count": 1},
                },
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            continue
        record = serialise_queue_record(reserved)
        if record is None:
            continue
        # This capability result is internal to the dispatcher. The general
        # serializer intentionally never exposes the opaque token or execution
        # envelope carried across the sessionless dispatch boundary.
        record["user_concept_id"] = reserved.get("user_concept_id")
        record["organisation_concept_id"] = reserved.get("organisation_concept_id")
        record["namespace"] = reserved.get("namespace")
        record["dispatch_reservation_token"] = token
        record["dispatch_owner"] = reserved.get("dispatch_owner")
        record["dispatch_acquired_at"] = _serialise_datetime(
            reserved.get("dispatch_acquired_at")
        )
        record["dispatch_heartbeat_at"] = _serialise_datetime(
            reserved.get("dispatch_heartbeat_at")
        )
        record["dispatch_lease_expires_at"] = _serialise_datetime(
            reserved.get("dispatch_lease_expires_at")
        )
        record["execution_envelope"] = (
            dict(reserved.get("execution_envelope"))
            if isinstance(reserved.get("execution_envelope"), Mapping)
            else {}
        )
        return record
    return None


def release_server_dispatch_reservation(
    *,
    queue_id: str,
    dispatch_reservation_token: str,
    server_instance_id: str,
    retry_after_seconds: float = 0,
    error: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Release an exact pre-admission reservation back to its FIFO."""

    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    token = _coerce_scope_value(
        dispatch_reservation_token,
        field="dispatch_reservation_token",
    )
    owner = _coerce_scope_value(server_instance_id, field="server_instance_id")
    try:
        delay = float(retry_after_seconds)
    except (TypeError, ValueError) as exc:
        raise InvalidChatPromptQueueInput(
            "retry_after_seconds must be a non-negative number"
        ) from exc
    if not math.isfinite(delay) or delay < 0:
        raise InvalidChatPromptQueueInput("retry_after_seconds must be a finite number")
    observed_now = now or _now()
    set_fields: dict[str, Any] = {
        "updated_at": observed_now,
        "next_dispatch_at": observed_now + timedelta(seconds=delay),
    }
    error_clean = _coerce_text(
        error,
        field="error",
        required=False,
        max_chars=4_000,
    )
    unset_fields: dict[str, str] = {
        "active_conversation_key": "",
        "dispatch_reservation_token": "",
        "dispatch_owner": "",
        "dispatch_acquired_at": "",
        "dispatch_heartbeat_at": "",
        "dispatch_lease_expires_at": "",
        "client_request_id": "",
        "attempt_id": "",
    }
    if error_clean is not None:
        set_fields["last_dispatch_error"] = error_clean
    else:
        unset_fields["last_dispatch_error"] = ""
    result = _collection().update_one(
        {
            "queue_id": queue_id_clean,
            "status": STATUS_QUEUED,
            "dispatch_mode": DISPATCH_MODE_SERVER,
            "dispatch_reservation_token": token,
            "dispatch_owner": owner,
            "active_conversation_key": {"$exists": True},
        },
        {"$set": set_fields, "$unset": unset_fields},
    )
    return bool(getattr(result, "matched_count", 0))


def heartbeat_server_dispatch_reservation(
    *,
    queue_id: str,
    dispatch_reservation_token: str,
    server_instance_id: str,
    now: datetime | None = None,
    lease_seconds: int | None = None,
) -> bool:
    """Renew one exact server dispatch reservation."""

    observed_now = now or _now()
    result = _collection().update_one(
        {
            "queue_id": _coerce_scope_value(queue_id, field="queue_id"),
            "status": STATUS_QUEUED,
            "dispatch_mode": DISPATCH_MODE_SERVER,
            "dispatch_reservation_token": _coerce_scope_value(
                dispatch_reservation_token,
                field="dispatch_reservation_token",
            ),
            "dispatch_owner": _coerce_scope_value(
                server_instance_id,
                field="server_instance_id",
            ),
            "active_conversation_key": {"$exists": True},
        },
        {
            "$set": {
                "dispatch_heartbeat_at": observed_now,
                "dispatch_lease_expires_at": observed_now
                + timedelta(seconds=_dispatch_lease_seconds(lease_seconds)),
                "updated_at": observed_now,
            }
        },
    )
    return bool(getattr(result, "matched_count", 0))


def fail_server_dispatch_reservation(
    *,
    queue_id: str,
    dispatch_reservation_token: str,
    server_instance_id: str,
    error: str,
    now: datetime | None = None,
) -> bool:
    """Terminalise an exact reservation that failed before turn admission."""

    observed_now = now or _now()
    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    token_clean = _coerce_scope_value(
        dispatch_reservation_token,
        field="dispatch_reservation_token",
    )
    owner_clean = _coerce_scope_value(
        server_instance_id,
        field="server_instance_id",
    )
    error_clean = _coerce_text(
        error,
        field="error",
        required=True,
        max_chars=4_000,
    )
    coll = _collection()
    exact_query = {
        "queue_id": queue_id_clean,
        "status": STATUS_QUEUED,
        "dispatch_mode": DISPATCH_MODE_SERVER,
        "dispatch_reservation_token": token_clean,
        "dispatch_owner": owner_clean,
        "active_conversation_key": {"$exists": True},
    }
    existing = coll.find_one(exact_query)
    reconciliation_set, reconciliation_unset = _terminal_reconciliation_update(
        existing,
        status=STATUS_FAILED,
        error=error_clean,
        source="server_dispatch_failure",
        observed_now=observed_now,
    )
    result = coll.update_one(
        exact_query,
        {
            "$set": {
                "status": STATUS_FAILED,
                "completed_at": observed_now,
                "updated_at": observed_now,
                "last_error": error_clean,
                **reconciliation_set,
            },
            "$unset": {
                "active_conversation_key": "",
                "active_task_execution_key": "",
                "dispatch_reservation_token": "",
                "dispatch_owner": "",
                "dispatch_acquired_at": "",
                "dispatch_heartbeat_at": "",
                "dispatch_lease_expires_at": "",
                "queued_global_slot": "",
                "queued_user_slot": "",
                "next_dispatch_at": "",
                **reconciliation_unset,
            },
        },
    )
    return bool(getattr(result, "matched_count", 0))


def _requeue_with_bounded_slots(
    *,
    coll: Collection,
    query: Mapping[str, Any],
    update: Mapping[str, Any],
    user_concept_id: str,
) -> Mapping[str, Any] | None:
    """Atomically re-admit one existing record to the bounded queued backlog."""

    global_limit, per_user_limit = _foreground_queue_limits()
    with _QUEUE_ADMISSION_LOCK:
        legacy_global = coll.count_documents(
            {"status": STATUS_QUEUED, "queued_global_slot": {"$exists": False}}
        )
        legacy_user = coll.count_documents(
            {
                "status": STATUS_QUEUED,
                "user_concept_id": user_concept_id,
                "queued_user_slot": {"$exists": False},
            }
        )
        available_global = max(0, global_limit - int(legacy_global))
        available_user = max(0, per_user_limit - int(legacy_user))
        occupied_global_slots = set(
            coll.distinct("queued_global_slot", {"status": STATUS_QUEUED})
        )
        occupied_user_slots = set(
            coll.distinct(
                "queued_user_slot",
                {"status": STATUS_QUEUED, "user_concept_id": user_concept_id},
            )
        )
        for user_slot in range(available_user):
            user_slot_key = _queued_user_slot_key(user_concept_id, user_slot)
            if user_slot_key in occupied_user_slots:
                continue
            for global_slot in range(available_global):
                if global_slot in occupied_global_slots:
                    continue
                candidate_update = {
                    key: dict(value) if isinstance(value, Mapping) else value
                    for key, value in update.items()
                }
                set_fields = dict(candidate_update.get("$set") or {})
                set_fields.update(
                    {
                        "queued_global_slot": global_slot,
                        "queued_user_slot": user_slot_key,
                    }
                )
                candidate_update["$set"] = set_fields
                try:
                    doc = coll.find_one_and_update(
                        dict(query),
                        candidate_update,
                        return_document=ReturnDocument.AFTER,
                    )
                except DuplicateKeyError:
                    continue
                if doc is not None:
                    return doc
                return None

        user_queued = coll.count_documents(
            {"status": STATUS_QUEUED, "user_concept_id": user_concept_id}
        )
        if user_queued >= per_user_limit:
            raise ChatPromptQueueCapacityReached(
                "This user has reached the queued-turn backlog limit",
                limit_kind="user",
            )
        raise ChatPromptQueueCapacityReached(
            "Von has reached the global queued-turn backlog limit",
            limit_kind="global",
        )


def bind_queue_record_to_turn(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
    client_request_id: str,
    conversation_key: str,
    server_instance_id: str,
    attempt_id: str | None = None,
    dispatch_reservation_token: str | None = None,
    lease_seconds: int | None = None,
) -> dict[str, Any]:
    """Atomically bind one queued prompt to the executing conversation turn.

    ``active_conversation_key`` is the cross-thread/process single-flight
    fence. A server dispatcher may reserve it before admission, but only the
    exact opaque reservation token owned by this server can upgrade that row.
    The stable ``conversation_key`` remains afterwards for reconciliation.
    """

    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    request_id_clean = _coerce_scope_value(
        client_request_id,
        field="client_request_id",
    )
    conversation_key_clean = _coerce_scope_value(
        conversation_key,
        field="conversation_key",
    )
    server_instance_id_clean = _coerce_scope_value(
        server_instance_id,
        field="server_instance_id",
    )
    dispatch_reservation_token_clean = _coerce_scope_value(
        dispatch_reservation_token,
        field="dispatch_reservation_token",
        required=False,
    )
    attempt_id_clean = _coerce_scope_value(
        attempt_id,
        field="attempt_id",
        required=False,
    ) or str(uuid4())
    now = _now()
    canonical_scope = _scope_query(scope)
    coll = _collection()

    existing = coll.find_one(
        {**_compatible_scope_query(scope), "queue_id": queue_id_clean}
    )
    if existing is None:
        raise _transition_not_found_error(
            scope=scope,
            queue_id=queue_id_clean,
            expected_statuses=ACTIVE_STATUSES,
            fallback_message="active prompt was not found",
        )
    existing_request_id = existing.get("client_request_id")
    if existing_request_id and existing_request_id != request_id_clean:
        raise InvalidChatPromptQueueInput(
            "prompt queue record is already bound to a different request"
        )
    persisted_reservation_token = _coerce_scope_value(
        existing.get("dispatch_reservation_token"),
        field="dispatch_reservation_token",
        required=False,
    )
    reservation_upgrade = bool(
        dispatch_reservation_token_clean
        and persisted_reservation_token == dispatch_reservation_token_clean
        and existing.get("dispatch_owner") == server_instance_id_clean
        and existing.get("status") == STATUS_QUEUED
    )
    if existing.get("active_conversation_key") and not reservation_upgrade:
        raise ConversationTurnAlreadyActive(
            "prompt queue record is already executing",
            queue_id=queue_id_clean,
        )
    if dispatch_reservation_token_clean and not reservation_upgrade:
        raise ConversationTurnAlreadyActive(
            "server dispatch reservation is no longer owned by this request",
            queue_id=queue_id_clean,
        )
    existing_attempt_id = _coerce_scope_value(
        existing.get("attempt_id"),
        field="attempt_id",
        required=False,
    )
    if reservation_upgrade and existing_attempt_id != attempt_id_clean:
        raise InvalidChatPromptQueueInput(
            "server dispatch reservation belongs to a different attempt"
        )

    persisted_conversation_key = _coerce_scope_value(
        existing.get("conversation_key"),
        field="conversation_key",
        required=False,
    )
    if (
        persisted_conversation_key
        and persisted_conversation_key != conversation_key_clean
    ):
        raise InvalidChatPromptQueueInput(
            "prompt queue record belongs to a different conversation"
        )

    # New records carry the server-computed canonical key from creation, so an
    # invitee and owner share one FIFO even though their visibility scopes differ.
    # Legacy rows retain their former same-scope session FIFO until first bound.
    session_id = existing.get("session_id")
    if persisted_conversation_key:
        earlier = coll.find_one(
            {
                "conversation_key": persisted_conversation_key,
                "status": {"$in": ACTIVE_STATUSES},
                "queue_id": {"$ne": queue_id_clean},
                "$and": [_earlier_enqueue_filter(existing)],
            },
            {
                "queue_id": 1,
                "user_concept_id": 1,
                "organisation_concept_id": 1,
                "namespace": 1,
            },
            sort=_enqueue_sort(),
        )
        if earlier is not None:
            same_scope_queue_id = (
                str(earlier.get("queue_id") or "") or None
                if _scope_matches(earlier, scope)
                else None
            )
            raise ConversationTurnAlreadyActive(
                "an earlier prompt is waiting for this conversation",
                queue_id=same_scope_queue_id,
            )
    elif session_id:
        earlier = coll.find_one(
            {
                **_compatible_scope_query(scope),
                "session_id": session_id,
                "status": {"$in": ACTIVE_STATUSES},
                "queue_id": {"$ne": queue_id_clean},
                "$and": [_earlier_enqueue_filter(existing)],
            },
            {"queue_id": 1},
            sort=_enqueue_sort(),
        )
        if earlier is not None:
            raise ConversationTurnAlreadyActive(
                "an earlier prompt is waiting for this conversation",
                queue_id=str(earlier.get("queue_id") or "") or None,
            )

    if reservation_upgrade:
        ownership_guard: dict[str, Any] = {
            "status": STATUS_QUEUED,
            "active_conversation_key": conversation_key_clean,
            "dispatch_reservation_token": dispatch_reservation_token_clean,
            "dispatch_owner": server_instance_id_clean,
            "dispatch_lease_expires_at": {"$gt": now},
        }
    else:
        ownership_guard = {
            "status": {"$in": ACTIVE_STATUSES},
            "active_conversation_key": {"$exists": False},
            "dispatch_reservation_token": {"$exists": False},
        }

    try:
        doc = coll.find_one_and_update(
            {
                **_compatible_scope_query(scope),
                "queue_id": queue_id_clean,
                "$or": [
                    {"client_request_id": None},
                    {"client_request_id": request_id_clean},
                    {"client_request_id": {"$exists": False}},
                ],
                **ownership_guard,
            },
            {
                "$set": {
                    **canonical_scope,
                    "status": STATUS_IN_PROGRESS,
                    "client_request_id": request_id_clean,
                    "attempt_id": attempt_id_clean,
                    "conversation_key": conversation_key_clean,
                    "active_conversation_key": conversation_key_clean,
                    "server_instance_id": server_instance_id_clean,
                    "claimed_at": now,
                    "lease_acquired_at": now,
                    "lease_heartbeat_at": now,
                    "lease_expires_at": now
                    + timedelta(seconds=_turn_lease_seconds(lease_seconds)),
                    "updated_at": now,
                    "completed_at": None,
                    "last_error": None,
                },
                "$unset": {
                    "queued_global_slot": "",
                    "queued_user_slot": "",
                    "stale_advisory": "",
                    "stale_advisory_at": "",
                    "stale_advisory_reason": "",
                    "reconciliation_required": "",
                    "dispatch_reservation_token": "",
                    "dispatch_owner": "",
                    "dispatch_acquired_at": "",
                    "dispatch_heartbeat_at": "",
                    "dispatch_lease_expires_at": "",
                    "next_dispatch_at": "",
                    "last_dispatch_error": "",
                },
                "$inc": {"attempt_count": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError as exc:
        raise ConversationTurnAlreadyActive(
            "another turn is active for this conversation"
        ) from exc

    record = serialise_queue_record(doc)
    if record is None:
        raise ConversationTurnAlreadyActive(
            "another turn acquired this prompt or conversation"
        )
    return record


def heartbeat_bound_turn(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
    attempt_id: str,
    server_instance_id: str | None = None,
    now: datetime | None = None,
    lease_seconds: int | None = None,
) -> bool:
    """Refresh an exact admitted turn lease without changing its semantics."""

    observed_now = now or _now()
    owner = _coerce_scope_value(
        server_instance_id,
        field="server_instance_id",
        required=False,
    )
    query: dict[str, Any] = {
        **_compatible_scope_query(scope),
        "queue_id": _coerce_scope_value(queue_id, field="queue_id"),
        "attempt_id": _coerce_scope_value(attempt_id, field="attempt_id"),
        "status": STATUS_IN_PROGRESS,
        "active_conversation_key": {"$exists": True},
    }
    if owner:
        query["server_instance_id"] = owner
    result = _collection().update_one(
        query,
        {
            "$set": {
                "lease_heartbeat_at": observed_now,
                "lease_expires_at": observed_now
                + timedelta(seconds=_turn_lease_seconds(lease_seconds)),
                "updated_at": observed_now,
            }
        },
    )
    return bool(getattr(result, "matched_count", 0))


def update_queued_record(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
    prompt_raw: str | None = None,
    session_id: Any = _UNSET,
    session_name: str | None = None,
    conversation_key: Any = _UNSET,
) -> dict[str, Any]:
    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    coll = _collection()
    if (
        coll.find_one(
            {
                **_compatible_scope_query(scope),
                "queue_id": queue_id_clean,
                "status": STATUS_QUEUED,
                "dispatch_mode": DISPATCH_MODE_SERVER,
            },
            {"_id": 1},
        )
        is not None
    ):
        raise InvalidChatPromptQueueInput(
            "server-dispatched queue records are immutable; cancel and enqueue new work"
        )
    set_fields: dict[str, Any] = {"updated_at": _now()}
    if prompt_raw is not None:
        set_fields["prompt_raw"] = _coerce_text(
            prompt_raw,
            field="prompt_raw",
            required=True,
            max_chars=MAX_PROMPT_RAW_CHARS,
        )
    if session_id is not _UNSET:
        set_fields["session_id"] = _coerce_scope_value(
            session_id, field="session_id", required=False
        )
        set_fields["conversation_key"] = _coerce_scope_value(
            None if conversation_key is _UNSET else conversation_key,
            field="conversation_key",
            required=False,
        )
    if session_name is not None:
        clean_session_name = _coerce_text(
            session_name,
            field="session_name",
            required=False,
            max_chars=MAX_SESSION_NAME_CHARS,
        )
        set_fields["session_name"] = (
            clean_session_name.strip() if clean_session_name else None
        )

    canonical_scope = _scope_query(scope)
    doc = coll.find_one_and_update(
        {
            **_compatible_scope_query(scope),
            "queue_id": queue_id_clean,
            "status": STATUS_QUEUED,
            "dispatch_mode": {"$ne": DISPATCH_MODE_SERVER},
        },
        {"$set": {**set_fields, **canonical_scope}},
        return_document=ReturnDocument.AFTER,
    )
    record = serialise_queue_record(doc)
    if record is None:
        raise _transition_not_found_error(
            scope=scope,
            queue_id=queue_id_clean,
            expected_statuses=[STATUS_QUEUED],
            fallback_message="queued prompt was not found",
        )
    return record


def claim_queue_record(*, scope: Mapping[str, Any], queue_id: str) -> dict[str, Any]:
    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    now = _now()
    canonical_scope = _scope_query(scope)
    coll = _collection()
    if (
        coll.find_one(
            {
                **_compatible_scope_query(scope),
                "queue_id": queue_id_clean,
                "status": STATUS_QUEUED,
                "dispatch_mode": DISPATCH_MODE_SERVER,
            },
            {"_id": 1},
        )
        is not None
    ):
        raise InvalidChatPromptQueueInput(
            "server-dispatched queue records require a server dispatch reservation"
        )
    doc = coll.find_one_and_update(
        {
            **_compatible_scope_query(scope),
            "queue_id": queue_id_clean,
            "status": STATUS_QUEUED,
            "dispatch_mode": {"$ne": DISPATCH_MODE_SERVER},
        },
        {
            "$set": {
                **canonical_scope,
                "status": STATUS_IN_PROGRESS,
                "claimed_at": now,
                "updated_at": now,
                "completed_at": None,
                "last_error": None,
            },
            "$unset": {
                "queued_global_slot": "",
                "queued_user_slot": "",
                "stale_advisory": "",
                "stale_advisory_at": "",
                "stale_advisory_reason": "",
                "reconciliation_required": "",
            },
            "$inc": {"attempt_count": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    record = serialise_queue_record(doc)
    if record is None:
        raise _transition_not_found_error(
            scope=scope,
            queue_id=queue_id_clean,
            expected_statuses=[STATUS_QUEUED],
            fallback_message="queued prompt was not found",
        )
    return record


def requeue_prompt_record(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
    current_server_instance_id: str | None = None,
) -> dict[str, Any]:
    """Return an interrupted record to FIFO without stealing a live lease.

    Legacy claimed rows have no durable conversation fence and remain directly
    restartable. A server-bound row may be released only after the one-server
    runtime identity has changed, which is the bounded restart signal for this
    architecture. The captured attempt and server identity are included in the
    update as a compare-and-set guard against a concurrent rebind.
    """

    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    now = _now()
    canonical_scope = _scope_query(scope)
    coll = _collection()
    existing = coll.find_one(
        {
            **_compatible_scope_query(scope),
            "queue_id": queue_id_clean,
            "status": STATUS_IN_PROGRESS,
        }
    )
    if existing is None:
        raise _transition_not_found_error(
            scope=scope,
            queue_id=queue_id_clean,
            expected_statuses=[STATUS_IN_PROGRESS],
            fallback_message="in-progress prompt was not found",
        )
    if existing.get("dispatch_mode") == DISPATCH_MODE_SERVER:
        raise ChatPromptQueueReconciliationRequired(
            "A server-dispatched attempt cannot be replayed without reconciling "
            "its exact turn and effect receipts"
        )
    if isinstance(existing.get("cancellation_requested_at"), datetime):
        raise ChatPromptQueueReconciliationRequired(
            "This turn has a durable cancellation request and cannot be replayed"
        )

    transition_guard: dict[str, Any] = {}
    if existing.get("active_conversation_key"):
        current_instance = _coerce_scope_value(
            current_server_instance_id,
            field="current_server_instance_id",
            required=False,
        )
        bound_instance = _coerce_scope_value(
            existing.get("server_instance_id"),
            field="server_instance_id",
            required=False,
        )
        if (
            not current_instance
            or not bound_instance
            or current_instance == bound_instance
        ):
            raise ConversationTurnAlreadyActive(
                "the server-bound turn is still owned by its executing server",
                queue_id=queue_id_clean,
            )
        transition_guard = {
            "active_conversation_key": existing.get("active_conversation_key"),
            "attempt_id": existing.get("attempt_id"),
            "server_instance_id": bound_instance,
        }
    else:
        transition_guard = {"active_conversation_key": {"$exists": False}}

    requeue_query = {
        **_compatible_scope_query(scope),
        "queue_id": queue_id_clean,
        "status": STATUS_IN_PROGRESS,
        **transition_guard,
    }
    requeue_update = {
        "$set": {
            **canonical_scope,
            "status": STATUS_QUEUED,
            "queued_at": now,
            "updated_at": now,
            "claimed_at": None,
            "completed_at": None,
            "last_error": None,
            "source": "restart",
        },
        "$unset": {
            "active_conversation_key": "",
            "client_request_id": "",
            "attempt_id": "",
            "server_instance_id": "",
            "lease_acquired_at": "",
            "lease_heartbeat_at": "",
            "lease_expires_at": "",
            "dispatch_reservation_token": "",
            "dispatch_owner": "",
            "dispatch_acquired_at": "",
            "dispatch_heartbeat_at": "",
            "dispatch_lease_expires_at": "",
            "next_dispatch_at": "",
            "last_dispatch_error": "",
            "purge_after": "",
            "stale_advisory": "",
            "stale_advisory_at": "",
            "stale_advisory_reason": "",
            "reconciliation_required": "",
        },
    }
    doc = _requeue_with_bounded_slots(
        coll=coll,
        query=requeue_query,
        update=requeue_update,
        user_concept_id=str(canonical_scope["user_concept_id"]),
    )
    record = serialise_queue_record(doc)
    if record is None:
        raise _transition_not_found_error(
            scope=scope,
            queue_id=queue_id_clean,
            expected_statuses=[STATUS_IN_PROGRESS],
            fallback_message="in-progress prompt was not found",
        )
    return record


def request_prompt_cancellation(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Persist cancellation intent without releasing a live conversation fence.

    A provider call cannot always be interrupted.  The executing turn retains
    its fence until it observes this request and terminalises, while a
    replacement one-server runtime may safely complete the cancellation after
    the former process has stopped.
    """

    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    attempt_id_clean = _coerce_scope_value(
        attempt_id,
        field="attempt_id",
        required=False,
    )
    query: dict[str, Any] = {
        **_compatible_scope_query(scope),
        "queue_id": queue_id_clean,
        "status": STATUS_IN_PROGRESS,
        "active_conversation_key": {"$exists": True},
    }
    if attempt_id_clean:
        query["attempt_id"] = attempt_id_clean

    coll = _collection()
    now = _now()
    doc = coll.find_one_and_update(
        {**query, "cancellation_requested_at": {"$exists": False}},
        {
            "$set": {
                **_scope_query(scope),
                "cancellation_requested_at": now,
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        # The exact same request is idempotent.  A terminal state is not
        # rewritten: its first durable outcome remains authoritative.
        doc = coll.find_one(
            {**query, "cancellation_requested_at": {"$type": "date"}}
        )
    record = serialise_queue_record(doc)
    if record is None:
        raise _transition_not_found_error(
            scope=scope,
            queue_id=queue_id_clean,
            expected_statuses=[STATUS_IN_PROGRESS],
            fallback_message="executing prompt was not available for cancellation",
        )
    return record


def is_prompt_cancellation_requested(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
    attempt_id: str | None = None,
) -> bool:
    """Return whether the exact admitted attempt has durable cancellation intent."""

    query: dict[str, Any] = {
        **_compatible_scope_query(scope),
        "queue_id": _coerce_scope_value(queue_id, field="queue_id"),
        "status": STATUS_IN_PROGRESS,
        "cancellation_requested_at": {"$type": "date"},
    }
    attempt_id_clean = _coerce_scope_value(
        attempt_id,
        field="attempt_id",
        required=False,
    )
    if attempt_id_clean:
        query["attempt_id"] = attempt_id_clean
    return _collection().find_one(query, {"_id": 1}) is not None


def terminalise_one_interrupted_cancellation(
    *,
    current_server_instance_id: str,
) -> dict[str, Any] | None:
    """Cancel one requested turn whose bound one-server runtime has stopped."""

    current_instance = _coerce_scope_value(
        current_server_instance_id,
        field="current_server_instance_id",
    )
    coll = _collection()
    existing = coll.find_one(
        {
            "status": STATUS_IN_PROGRESS,
            "active_conversation_key": {"$exists": True},
            "attempt_id": {"$type": "string", "$ne": ""},
            "server_instance_id": {"$type": "string", "$ne": current_instance},
            "cancellation_requested_at": {"$type": "date"},
        },
        sort=[("cancellation_requested_at", ASCENDING), ("_id", ASCENDING)],
    )
    if not isinstance(existing, Mapping):
        return None
    return finish_prompt_record(
        scope=_queue_scope_values(existing),
        queue_id=str(existing.get("queue_id") or ""),
        status=STATUS_CANCELLED,
        attempt_id=str(existing.get("attempt_id") or ""),
    )


def finish_prompt_record(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
    status: str = STATUS_COMPLETED,
    error: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    if status not in TERMINAL_STATUSES:
        raise InvalidChatPromptQueueInput("status must be a terminal queue status")
    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    now = _now()
    canonical_scope = _scope_query(scope)
    attempt_id_clean = _coerce_scope_value(
        attempt_id,
        field="attempt_id",
        required=False,
    )
    ownership_guard: dict[str, Any] = (
        {"attempt_id": attempt_id_clean}
        if attempt_id_clean
        else {"active_conversation_key": {"$exists": False}}
    )
    coll = _collection()
    transition_query = {
        **_compatible_scope_query(scope),
        "queue_id": queue_id_clean,
        "status": {"$in": ACTIVE_STATUSES},
        **ownership_guard,
    }
    existing_for_terminal = coll.find_one(transition_query)
    if not attempt_id_clean:
        existing = existing_for_terminal
        if (
            isinstance(existing, Mapping)
            and existing.get("dispatch_mode") == DISPATCH_MODE_SERVER
        ):
            raise ChatPromptQueueReconciliationRequired(
                "A server-dispatched queue row can only be terminalised by its "
                "exact admitted attempt"
            )
    if (
        isinstance(existing_for_terminal, Mapping)
        and isinstance(existing_for_terminal.get("cancellation_requested_at"), datetime)
    ):
        # Once durable cancellation won the CAS, a late provider response may
        # not publish completion or failure for the same exact attempt.
        status = STATUS_CANCELLED
        error = None
    error_clean = _coerce_text(
        error,
        field="error",
        required=False,
        max_chars=4_000,
    )
    reconciliation_set, reconciliation_unset = _terminal_reconciliation_update(
        existing_for_terminal,
        status=status,
        error=error_clean,
        source="queue_terminalisation",
        observed_now=now,
    )
    doc = coll.find_one_and_update(
        transition_query,
        {
            "$set": {
                **canonical_scope,
                "status": status,
                "updated_at": now,
                "completed_at": now,
                "last_error": error_clean,
                **reconciliation_set,
            },
            "$unset": {
                "active_conversation_key": "",
                "active_task_execution_key": "",
                "queued_global_slot": "",
                "queued_user_slot": "",
                "server_instance_id": "",
                "lease_acquired_at": "",
                "lease_heartbeat_at": "",
                "lease_expires_at": "",
                "dispatch_reservation_token": "",
                "dispatch_owner": "",
                "dispatch_acquired_at": "",
                "dispatch_heartbeat_at": "",
                "dispatch_lease_expires_at": "",
                "next_dispatch_at": "",
                "last_dispatch_error": "",
                "stale_advisory": "",
                "stale_advisory_at": "",
                "stale_advisory_reason": "",
                "reconciliation_required": "",
                **reconciliation_unset,
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    record = serialise_queue_record(doc)
    if record is None and attempt_id_clean:
        # A retry after an unknown Mongo acknowledgement must recognise the
        # exact already-committed attempt instead of stranding the in-process
        # token and its capacity reservation.
        record = serialise_queue_record(
            coll.find_one(
                {
                    **_compatible_scope_query(scope),
                    "queue_id": queue_id_clean,
                    "attempt_id": attempt_id_clean,
                    "status": status,
                }
            )
        )
    if record is None and not attempt_id_clean:
        # Pre-#473 clients terminalise the record in a finally block after the
        # server-owned generate lifecycle has already done so.  Limit this
        # idempotent acknowledgement to an exact, fully joined legacy pair.
        record = serialise_queue_record(
            coll.find_one(
                {
                    **_compatible_scope_query(scope),
                    "queue_id": queue_id_clean,
                    "status": status,
                    "legacy_submission_queue_seen": True,
                    "legacy_submission_generate_seen": True,
                }
            )
        )
    if record is None:
        raise _transition_not_found_error(
            scope=scope,
            queue_id=queue_id_clean,
            expected_statuses=ACTIVE_STATUSES,
            fallback_message="active prompt was not found",
        )
    return record


def cancel_prompt_record(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
    handoff_replacement_queue_id: str | None = None,
) -> dict[str, Any]:
    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    replacement_queue_id_clean = _coerce_scope_value(
        handoff_replacement_queue_id,
        field="handoff_replacement_queue_id",
        required=False,
    )
    now = _now()
    canonical_scope = _scope_query(scope)

    coll = _collection()
    existing = coll.find_one(
        {**_compatible_scope_query(scope), "queue_id": queue_id_clean}
    )
    if (
        replacement_queue_id_clean
        and isinstance(existing, Mapping)
        and existing.get("status") == STATUS_CANCELLED
    ):
        if existing.get("handoff_replacement_queue_id") == replacement_queue_id_clean:
            record = serialise_queue_record(existing)
            if record is not None:
                return record
        raise ChatPromptQueueHandoffConflict(
            "The handoff source was cancelled independently of this replacement",
            queue_id=replacement_queue_id_clean,
        )
    if (
        existing is not None
        and existing.get("status") == STATUS_IN_PROGRESS
        and existing.get("active_conversation_key")
    ):
        raise ConversationTurnAlreadyActive(
            "an executing turn must be cancelled through its bound task",
            queue_id=queue_id_clean,
        )

    reconciliation_set, reconciliation_unset = _terminal_reconciliation_update(
        existing,
        status=STATUS_CANCELLED,
        error=None,
        source="queue_cancellation",
        observed_now=now,
    )
    if (
        isinstance(existing, Mapping)
        and existing.get("status") == STATUS_FAILED
        and not _has_task_execution_link(existing)
        and isinstance(existing.get("completed_at"), datetime)
    ):
        reconciliation_set["purge_after"] = _terminal_purge_after(
            existing["completed_at"]
        )
    unresolved_handoff_source_id = (
        str(existing.get("handoff_source_queue_id") or "").strip()
        if (
            isinstance(existing, Mapping)
            and existing.get("status") == STATUS_QUEUED
            and existing.get("dispatch_mode") == DISPATCH_MODE_SERVER
            and existing.get("dispatch_ready") is False
        )
        else ""
    )
    if unresolved_handoff_source_id:
        # Release the active unique fence immediately while retaining terminal
        # provenance. A cancelled replacement must not block a safe retry of a
        # source that the caller deliberately left queued.
        reconciliation_set.update(
            {
                "retired_handoff_source_queue_id": unresolved_handoff_source_id,
                "handoff_cancelled_at": now,
            }
        )
        reconciliation_unset.update(
            {
                "handoff_source_queue_id": "",
                "handoff_reconciliation_attempted_at": "",
                "handoff_reconciliation_next_at": "",
                "handoff_reconciliation_attempt_count": "",
                "handoff_reconciliation_last_error": "",
            }
        )
    if replacement_queue_id_clean:
        reconciliation_set.update(
            {
                "handoff_replacement_queue_id": replacement_queue_id_clean,
                "handoff_source_retired_at": now,
            }
        )

    cancellable_query: dict[str, Any] = {
        **_compatible_scope_query(scope),
        "queue_id": queue_id_clean,
        "$or": [
            {"status": STATUS_QUEUED},
            {
                "status": STATUS_IN_PROGRESS,
                "active_conversation_key": {"$exists": False},
            },
        ],
    }
    if replacement_queue_id_clean:
        cancellable_query["handoff_replacement_queue_id"] = {"$exists": False}

    doc = coll.find_one_and_update(
        cancellable_query,
        {
            "$set": {
                **canonical_scope,
                "status": STATUS_CANCELLED,
                "updated_at": now,
                "completed_at": now,
                "last_error": None,
                **reconciliation_set,
            },
            "$unset": {
                "active_conversation_key": "",
                "active_task_execution_key": "",
                "queued_global_slot": "",
                "queued_user_slot": "",
                "server_instance_id": "",
                "lease_acquired_at": "",
                "lease_heartbeat_at": "",
                "lease_expires_at": "",
                "dispatch_reservation_token": "",
                "dispatch_owner": "",
                "dispatch_acquired_at": "",
                "dispatch_heartbeat_at": "",
                "dispatch_lease_expires_at": "",
                "next_dispatch_at": "",
                "last_dispatch_error": "",
                "stale_advisory": "",
                "stale_advisory_at": "",
                "stale_advisory_reason": "",
                "reconciliation_required": "",
                **reconciliation_unset,
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        # Failed records keep their original completion time and diagnostic
        # evidence when explicitly dismissed.
        failed_query: dict[str, Any] = {
            **_compatible_scope_query(scope),
            "queue_id": queue_id_clean,
            "status": STATUS_FAILED,
        }
        if replacement_queue_id_clean:
            failed_query["handoff_replacement_queue_id"] = {"$exists": False}
        doc = coll.find_one_and_update(
            failed_query,
            {
                "$set": {
                    **canonical_scope,
                    "status": STATUS_CANCELLED,
                    "updated_at": now,
                    **reconciliation_set,
                },
                "$unset": {
                    "active_task_execution_key": "",
                    **reconciliation_unset,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
    if doc is None and replacement_queue_id_clean:
        retired = coll.find_one(
            {
                **_compatible_scope_query(scope),
                "queue_id": queue_id_clean,
                "status": STATUS_CANCELLED,
            }
        )
        if (
            isinstance(retired, Mapping)
            and retired.get("handoff_replacement_queue_id")
            == replacement_queue_id_clean
        ):
            record = serialise_queue_record(retired)
            if record is not None:
                return record
        if isinstance(retired, Mapping):
            raise ChatPromptQueueHandoffConflict(
                "The handoff source was cancelled independently of this replacement",
                queue_id=replacement_queue_id_clean,
            )
    record = serialise_queue_record(doc)
    if record is None:
        raise _transition_not_found_error(
            scope=scope,
            queue_id=queue_id_clean,
            expected_statuses=CANCELLABLE_STATUSES,
            fallback_message="cancellable prompt was not found",
        )
    return record
