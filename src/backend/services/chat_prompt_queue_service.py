"""Server-side persistence for chat prompts queued behind active turns.

This service is a support surface: it stores and transitions prompt queue
records, but it does not decide what Von should answer or how workflows run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.collection import Collection

from ..db.mongo_client import get_chat_prompt_queue_collection

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


class ChatPromptQueueError(RuntimeError):
    """Base error for chat prompt queue operations."""


class ChatPromptQueueUnavailable(ChatPromptQueueError):
    """Raised when the persistence backend is unavailable."""


class InvalidChatPromptQueueInput(ChatPromptQueueError, ValueError):
    """Raised when a caller supplies invalid queue data."""


class ChatPromptQueueRecordNotFound(ChatPromptQueueError, LookupError):
    """Raised when a queue record is not in the expected scope/state."""


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

    user_id = _coerce_scope_value(user_concept_id, field="user_concept_id")
    org_id = _coerce_scope_value(
        organisation_concept_id, field="organisation_concept_id", required=False
    )
    scope_namespace = _coerce_scope_value(namespace, field="namespace", required=False)
    return {
        "user_concept_id": user_id,
        "organisation_concept_id": org_id,
        "namespace": scope_namespace,
    }


def _scope_query(scope: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "user_concept_id": _coerce_scope_value(scope.get("user_concept_id"), field="user_concept_id"),
        "organisation_concept_id": _coerce_scope_value(
            scope.get("organisation_concept_id"),
            field="organisation_concept_id",
            required=False,
        ),
        "namespace": _coerce_scope_value(scope.get("namespace"), field="namespace", required=False),
    }


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
    query = {
        **_scope_query(scope),
        "status": {"$in": allowed_statuses},
    }
    docs = _collection().find(query).sort([("created_at", 1)]).limit(max_limit)
    return [record for record in (serialise_queue_record(doc) for doc in docs) if record]


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

    doc = _collection().find_one_and_update(
        {
            **_scope_query(scope),
            "queue_id": queue_id_clean,
            "status": STATUS_QUEUED,
        },
        {"$set": set_fields},
        return_document=ReturnDocument.AFTER,
    )
    record = serialise_queue_record(doc)
    if record is None:
        raise ChatPromptQueueRecordNotFound("queued prompt was not found")
    return record


def claim_queue_record(*, scope: Mapping[str, Any], queue_id: str) -> dict[str, Any]:
    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    now = _now()
    doc = _collection().find_one_and_update(
        {
            **_scope_query(scope),
            "queue_id": queue_id_clean,
            "status": STATUS_QUEUED,
        },
        {
            "$set": {
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
        raise ChatPromptQueueRecordNotFound("queued prompt was not found")
    return record


def requeue_prompt_record(*, scope: Mapping[str, Any], queue_id: str) -> dict[str, Any]:
    queue_id_clean = _coerce_scope_value(queue_id, field="queue_id")
    now = _now()
    doc = _collection().find_one_and_update(
        {
            **_scope_query(scope),
            "queue_id": queue_id_clean,
            "status": STATUS_IN_PROGRESS,
        },
        {
            "$set": {
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
        raise ChatPromptQueueRecordNotFound("in-progress prompt was not found")
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
    doc = _collection().find_one_and_update(
        {
            **_scope_query(scope),
            "queue_id": queue_id_clean,
            "status": {"$in": ACTIVE_STATUSES},
        },
        {
            "$set": {
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
        raise ChatPromptQueueRecordNotFound("active prompt was not found")
    return record


def cancel_prompt_record(*, scope: Mapping[str, Any], queue_id: str) -> dict[str, Any]:
    return finish_prompt_record(scope=scope, queue_id=queue_id, status=STATUS_CANCELLED)
