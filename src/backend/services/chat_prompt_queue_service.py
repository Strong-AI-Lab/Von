"""Server-side persistence for chat prompts queued behind active turns.

This service is a support surface: it stores and transitions prompt queue
records, but it does not decide what Von should answer or how workflows run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.collection import Collection

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
VALID_STATUSES = set(ACTIVE_STATUSES + TERMINAL_STATUSES)
MAX_PROMPT_RAW_CHARS = 100_000
MAX_SESSION_NAME_CHARS = 500
STALE_IN_PROGRESS_TIMEOUT_SECONDS = 24 * 60 * 60
RECENT_FAILED_VISIBILITY_SECONDS = 24 * 60 * 60
STALE_IN_PROGRESS_LAST_ERROR = (
    "Prompt queue record expired after being in progress for more than 24 hours."
)


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_text(value: Any, *, field: str, required: bool, max_chars: int | None = None) -> str | None:
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
    """Mark old in-progress records terminal so active queue state cannot persist forever."""

    seconds = max(1, int(stale_after_seconds or STALE_IN_PROGRESS_TIMEOUT_SECONDS))
    cutoff = (now or _now()) - timedelta(seconds=seconds)
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
                "status": STATUS_FAILED,
                "updated_at": now or _now(),
                "completed_at": now or _now(),
                "last_error": STALE_IN_PROGRESS_LAST_ERROR,
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
    }


def list_active_queue_records(
    *,
    scope: Mapping[str, Any],
    statuses: Sequence[str] = ACTIVE_STATUSES,
    limit: int = 100,
) -> list[dict[str, Any]]:
    allowed_statuses = [str(status) for status in statuses if str(status) in VALID_STATUSES]
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
    return [record for record in (serialise_queue_record(doc) for doc in docs) if record]


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
    return [record for record in (serialise_queue_record(doc) for doc in docs) if record]


def list_queue_visibility_records(*, scope: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
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
) -> dict[str, Any]:
    if status not in {STATUS_QUEUED, STATUS_IN_PROGRESS}:
        raise InvalidChatPromptQueueInput("status must be queued or in_progress")
    prompt = _coerce_text(
        prompt_raw,
        field="prompt_raw",
        required=True,
        max_chars=MAX_PROMPT_RAW_CHARS,
    )
    session_id_clean = _coerce_scope_value(session_id, field="session_id", required=False)
    session_name_clean = _coerce_text(
        session_name,
        field="session_name",
        required=False,
        max_chars=MAX_SESSION_NAME_CHARS,
    )
    if session_name_clean is not None:
        session_name_clean = session_name_clean.strip() or None
    now = _now()
    doc = {
        "queue_id": str(uuid4()),
        **_scope_query(scope),
        "prompt_raw": prompt,
        "status": status,
        "session_id": session_id_clean,
        "session_name": session_name_clean,
        "source": source,
        "attempt_count": 1 if status == STATUS_IN_PROGRESS else 0,
        "created_at": now,
        "updated_at": now,
        "queued_at": now if status == STATUS_QUEUED else None,
        "claimed_at": now if status == STATUS_IN_PROGRESS else None,
        "completed_at": None,
        "last_error": None,
    }
    _collection().insert_one(doc)
    record = serialise_queue_record(doc)
    if record is None:  # pragma: no cover - defensive
        raise ChatPromptQueueUnavailable("created queue record could not be serialised")
    return record


def update_queued_record(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
    prompt_raw: str | None = None,
    session_id: str | None = None,
    session_name: str | None = None,
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
    if session_id is not None:
        set_fields["session_id"] = _coerce_scope_value(session_id, field="session_id", required=False)
    if session_name is not None:
        clean_session_name = _coerce_text(
            session_name,
            field="session_name",
            required=False,
            max_chars=MAX_SESSION_NAME_CHARS,
        )
        set_fields["session_name"] = clean_session_name.strip() if clean_session_name else None

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


def requeue_prompt_record(*, scope: Mapping[str, Any], queue_id: str) -> dict[str, Any]:
    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    now = _now()
    canonical_scope = _scope_query(scope)
    doc = _collection().find_one_and_update(
        {
            **_compatible_scope_query(scope),
            "queue_id": queue_id_clean,
            "status": STATUS_IN_PROGRESS,
        },
        {
            "$set": {
                **canonical_scope,
                "status": STATUS_QUEUED,
                "queued_at": now,
                "updated_at": now,
                "claimed_at": None,
                "completed_at": None,
                "last_error": None,
                "source": "restart",
            }
        },
        return_document=ReturnDocument.AFTER,
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
) -> dict[str, Any]:
    if status not in TERMINAL_STATUSES:
        raise InvalidChatPromptQueueInput("status must be a terminal queue status")
    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    now = _now()
    canonical_scope = _scope_query(scope)
    doc = _collection().find_one_and_update(
        {
            **_compatible_scope_query(scope),
            "queue_id": queue_id_clean,
            "status": {"$in": ACTIVE_STATUSES},
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
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    record = serialise_queue_record(doc)
    if record is None:
        raise _transition_not_found_error(
            scope=scope,
            queue_id=queue_id_clean,
            expected_statuses=ACTIVE_STATUSES,
            fallback_message="active prompt was not found",
        )
    return record


def cancel_prompt_record(*, scope: Mapping[str, Any], queue_id: str) -> dict[str, Any]:
    return finish_prompt_record(scope=scope, queue_id=queue_id, status=STATUS_CANCELLED)
