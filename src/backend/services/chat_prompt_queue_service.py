"""Server-side persistence for chat prompts queued behind active turns.

This service is a support surface: it stores and transitions prompt queue
records, but it does not decide what Von should answer or how workflows run.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from ..db.mongo_client import get_chat_prompt_queue_collection
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


def _queued_user_slot_key(user_concept_id: str, slot: int) -> str:
    actor_hash = hashlib.sha256(user_concept_id.encode("utf-8")).hexdigest()
    return f"{actor_hash}:{slot}"


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
                {"claimed_at": {"$lte": cutoff}},
                {"claimed_at": None, "updated_at": {"$lte": cutoff}},
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
        "client_request_id": doc.get("client_request_id"),
        "attempt_id": doc.get("attempt_id"),
        "conversation_key": doc.get("conversation_key"),
        "server_instance_id": doc.get("server_instance_id"),
        "lease_acquired_at": _serialise_datetime(doc.get("lease_acquired_at")),
        "lease_heartbeat_at": _serialise_datetime(doc.get("lease_heartbeat_at")),
        "stale_advisory": bool(doc.get("stale_advisory")),
        "stale_advisory_at": _serialise_datetime(doc.get("stale_advisory_at")),
        "stale_advisory_reason": doc.get("stale_advisory_reason"),
        "reconciliation_required": bool(doc.get("reconciliation_required")),
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
    docs = _collection().find(query).sort([("created_at", 1)]).limit(max_limit)
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
    }
    docs = (
        _collection()
        .find(query)
        .sort([("completed_at", -1), ("updated_at", -1)])
        .limit(max_limit)
    )
    return [
        record for record in (serialise_queue_record(doc) for doc in docs) if record
    ]


def list_queue_visibility_records(
    *, scope: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """Return active queue records plus bounded failed records for UI visibility."""

    return {
        "items": list_active_queue_records(scope=scope),
        "recent_failed_items": list_recent_failed_queue_records(scope=scope),
    }


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
    record = serialise_queue_record(doc)
    if record is None:  # pragma: no cover - defensive
        raise ChatPromptQueueUnavailable("created queue record could not be serialised")
    return record


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
) -> dict[str, Any]:
    """Atomically bind one queued prompt to the executing conversation turn.

    ``active_conversation_key`` exists only for the admitted record. Its unique
    partial index is the cross-thread/process single-flight fence; the stable
    ``conversation_key`` remains afterwards for reconciliation.
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
    if existing.get("active_conversation_key"):
        raise ConversationTurnAlreadyActive(
            "prompt queue record is already executing",
            queue_id=queue_id_clean,
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
        created_at = existing.get("created_at", now)
        earlier = coll.find_one(
            {
                "conversation_key": persisted_conversation_key,
                "status": {"$in": ACTIVE_STATUSES},
                "queue_id": {"$ne": queue_id_clean},
                "$or": [
                    {"created_at": {"$lt": created_at}},
                    {
                        "created_at": created_at,
                        "queue_id": {"$lt": queue_id_clean},
                    },
                ],
            },
            {
                "queue_id": 1,
                "user_concept_id": 1,
                "organisation_concept_id": 1,
                "namespace": 1,
            },
            sort=[("created_at", 1), ("queue_id", 1)],
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
                "created_at": {"$lt": existing.get("created_at", now)},
            },
            {"queue_id": 1},
            sort=[("created_at", 1), ("queue_id", 1)],
        )
        if earlier is not None:
            raise ConversationTurnAlreadyActive(
                "an earlier prompt is waiting for this conversation",
                queue_id=str(earlier.get("queue_id") or "") or None,
            )

    try:
        doc = coll.find_one_and_update(
            {
                **_compatible_scope_query(scope),
                "queue_id": queue_id_clean,
                "status": {"$in": ACTIVE_STATUSES},
                "$or": [
                    {"client_request_id": None},
                    {"client_request_id": request_id_clean},
                    {"client_request_id": {"$exists": False}},
                ],
                "active_conversation_key": {"$exists": False},
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
) -> bool:
    """Refresh an exact admitted turn lease without changing its semantics."""

    now = _now()
    result = _collection().update_one(
        {
            **_compatible_scope_query(scope),
            "queue_id": _coerce_scope_value(queue_id, field="queue_id"),
            "attempt_id": _coerce_scope_value(attempt_id, field="attempt_id"),
            "status": STATUS_IN_PROGRESS,
            "active_conversation_key": {"$exists": True},
        },
        {"$set": {"lease_heartbeat_at": now, "updated_at": now}},
    )
    return bool(getattr(result, "modified_count", 0))


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
    doc = _collection().find_one_and_update(
        {
            **_compatible_scope_query(scope),
            "queue_id": queue_id_clean,
            "status": STATUS_QUEUED,
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
    doc = _collection().find_one_and_update(
        {
            **_compatible_scope_query(scope),
            "queue_id": queue_id_clean,
            "status": STATUS_QUEUED,
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
    doc = coll.find_one_and_update(
        {
            **_compatible_scope_query(scope),
            "queue_id": queue_id_clean,
            "status": {"$in": ACTIVE_STATUSES},
            **ownership_guard,
        },
        {
            "$set": {
                **canonical_scope,
                "status": status,
                "updated_at": now,
                "completed_at": now,
                "last_error": _coerce_text(
                    error,
                    field="error",
                    required=False,
                    max_chars=4_000,
                ),
            },
            "$unset": {
                "active_conversation_key": "",
                "queued_global_slot": "",
                "queued_user_slot": "",
                "server_instance_id": "",
                "lease_acquired_at": "",
                "lease_heartbeat_at": "",
                "stale_advisory": "",
                "stale_advisory_at": "",
                "stale_advisory_reason": "",
                "reconciliation_required": "",
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


def cancel_prompt_record(*, scope: Mapping[str, Any], queue_id: str) -> dict[str, Any]:
    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    now = _now()
    canonical_scope = _scope_query(scope)

    coll = _collection()
    existing = coll.find_one(
        {**_compatible_scope_query(scope), "queue_id": queue_id_clean}
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

    doc = coll.find_one_and_update(
        {
            **_compatible_scope_query(scope),
            "queue_id": queue_id_clean,
            "$or": [
                {"status": STATUS_QUEUED},
                {
                    "status": STATUS_IN_PROGRESS,
                    "active_conversation_key": {"$exists": False},
                },
            ],
        },
        {
            "$set": {
                **canonical_scope,
                "status": STATUS_CANCELLED,
                "updated_at": now,
                "completed_at": now,
                "last_error": None,
            },
            "$unset": {
                "active_conversation_key": "",
                "queued_global_slot": "",
                "queued_user_slot": "",
                "server_instance_id": "",
                "lease_acquired_at": "",
                "lease_heartbeat_at": "",
                "stale_advisory": "",
                "stale_advisory_at": "",
                "stale_advisory_reason": "",
                "reconciliation_required": "",
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        # Failed records keep their original completion time and diagnostic
        # evidence when explicitly dismissed.
        doc = coll.find_one_and_update(
            {
                **_compatible_scope_query(scope),
                "queue_id": queue_id_clean,
                "status": STATUS_FAILED,
            },
            {
                "$set": {
                    **canonical_scope,
                    "status": STATUS_CANCELLED,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
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
