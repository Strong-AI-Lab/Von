"""Chat history service for persistent conversation storage."""

import hashlib
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Iterable
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import (
    PyMongoError,
)
from pymongo.read_preferences import ReadPreference
from ..db.mongo_client import get_db
from ..db.transient_errors import is_transient_mongo_error
from ..models.chat_history_model import chat_history_collection_name
from .turn_execution_record_service import (
    build_turn_execution_record,
    infer_turn_execution_workflow_routing_from_debug,
    upsert_turn_execution_record_projection,
)
from .episode_critique_memory_service import schedule_episode_critique_memory_from_turn
from .coding_agent_identity_bootstrap_service import (
    CODING_AGENT_TYPE_ID,
    GITHUB_COPILOT_INSTANCE_ID,
    VON_SYSTEM_ID,
)
from .debug_payload_store import (
    compact_debug_payload_for_storage,
    hydrate_debug_payload_blob_refs,
)
from .mongo_observability_service import (
    build_mongo_operation_comment,
    observe_mongo_operation,
)

# Try to import RAG service, but don't fail if it's not available (circular imports etc)
try:
    from .rag_service import get_rag_service
except ImportError:
    get_rag_service = None

logger = logging.getLogger(__name__)


_DETERMINISTIC_RAG_DOC_NAMESPACE = uuid.UUID("8c5a7fa9-9a7c-4f0f-8c1f-f4ad7f9f6fd7")
_SESSION_NAME_MAX_LEN = 80
_CHAT_HISTORY_INDEXES_READY = False
_CHAT_HISTORY_INDEXES_LOCK = threading.Lock()
_CHAT_HISTORY_READ_CIRCUIT_LOCK = threading.Lock()
_CHAT_HISTORY_READ_CIRCUIT_UNTIL_MONOTONIC = 0.0
_CHAT_HISTORY_READ_CIRCUIT_LAST_ERROR: Optional[str] = None
_CHAT_HISTORY_LIGHT_SESSION_METADATA_INDEX_NAME = (
    "namespace_user_recency_session_metadata_v1"
)
CHAT_SESSION_ORIGIN_KIND_BROWSER_TEST_FIXTURE = "browser_test_fixture"
CHAT_SESSION_ORIGIN_KIND_BENCHMARK_HARNESS = "benchmark_harness"
CHAT_SESSION_ORIGIN_KIND_CODING_AGENT_TEST = "coding_agent_test"
CHAT_SESSION_AGENT_CREATED_ORIGIN_KINDS = frozenset(
    {
        CHAT_SESSION_ORIGIN_KIND_BROWSER_TEST_FIXTURE,
        CHAT_SESSION_ORIGIN_KIND_BENCHMARK_HARNESS,
        CHAT_SESSION_ORIGIN_KIND_CODING_AGENT_TEST,
        "coding_agent",
        "agent_test",
    }
)
CHAT_SESSION_PROVENANCE_FIELDS = (
    "origin_kind",
    "created_by_actor_concept_id",
    "created_by_actor_type",
    "is_agent_created",
    "test_artifact_kind",
)
CHAT_SESSION_AGENT_VISIBILITY_EXCLUDE = "exclude"
CHAT_SESSION_AGENT_VISIBILITY_INCLUDE = "include"
CHAT_SESSION_AGENT_VISIBILITY_ONLY = "only"
VALID_CHAT_SESSION_AGENT_VISIBILITY_VALUES = frozenset(
    {
        CHAT_SESSION_AGENT_VISIBILITY_EXCLUDE,
        CHAT_SESSION_AGENT_VISIBILITY_INCLUDE,
        CHAT_SESSION_AGENT_VISIBILITY_ONLY,
    }
)
_BROWSER_TEST_SESSION_ID_PREFIX = "browser-fixture-"
_BENCHMARK_SESSION_NAME_PREFIX = "Benchmark session "
_LIVE_KB_TOOL_PROMPT_SAMPLER_SESSION_NAME_PREFIX = (
    "JVNAUTOSCI-1894 live prompt sample"
)
_LIVE_KB_TOOL_PROMPT_SAMPLER_TEST_ARTIFACT_KIND = (
    "live_kb_tool_prompt_sampler_chat_session"
)


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on", "y"}:
        return True
    if value in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _is_agent_test_instance() -> bool:
    return _parse_bool_env("VON_AGENT_TEST_INSTANCE", False)


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _chat_history_use_primary_preferred_reads() -> bool:
    return _parse_bool_env("VON_CHAT_HISTORY_PRIMARY_PREFERRED_READS", True)


def _chat_history_read_max_time_ms() -> int:
    return _positive_int_env("VON_CHAT_HISTORY_READ_MAX_TIME_MS", 2200)


def _chat_history_read_circuit_seconds() -> float:
    return _positive_float_env("VON_CHAT_HISTORY_READ_CIRCUIT_SECONDS", 3.0)


def _is_transient_chat_history_error(exc: Exception) -> bool:
    if is_transient_mongo_error(exc):
        return True
    message = str(exc).lower()
    transient_markers = ("read circuit open",)
    return any(marker in message for marker in transient_markers)


def is_transient_chat_history_error(exc: Exception) -> bool:
    """Expose transient-error classification for route-level fail-soft behaviour."""
    return _is_transient_chat_history_error(exc)


def get_chat_history_read_max_time_ms() -> int:
    """Expose read max-time for route-level point reads in conversation endpoints."""
    return _chat_history_read_max_time_ms()


def find_chat_history_document_for_read(
    chat_history_coll,
    query: Dict[str, Any],
    projection: Optional[Dict[str, Any]] = None,
):
    """Run a bounded read-only find_one against chat_history."""
    return _read_find_one(chat_history_coll, query, projection)


def _current_chat_history_read_circuit_remaining_seconds() -> float:
    now = time.monotonic()
    with _CHAT_HISTORY_READ_CIRCUIT_LOCK:
        return max(0.0, _CHAT_HISTORY_READ_CIRCUIT_UNTIL_MONOTONIC - now)


def _guard_chat_history_read(op_name: str) -> None:
    remaining = _current_chat_history_read_circuit_remaining_seconds()
    if remaining <= 0:
        return
    with _CHAT_HISTORY_READ_CIRCUIT_LOCK:
        last_error = _CHAT_HISTORY_READ_CIRCUIT_LAST_ERROR
    detail = f"; last_error={last_error}" if last_error else ""
    raise ChatHistoryServiceError(
        f"Chat history temporarily unavailable for {op_name}; "
        f"read circuit open for {remaining:.1f}s{detail}"
    )


def _record_chat_history_read_failure(op_name: str, exc: Exception) -> None:
    if not _is_transient_chat_history_error(exc):
        return
    cooldown_seconds = _chat_history_read_circuit_seconds()
    opened_until = time.monotonic() + cooldown_seconds
    with _CHAT_HISTORY_READ_CIRCUIT_LOCK:
        global _CHAT_HISTORY_READ_CIRCUIT_UNTIL_MONOTONIC
        global _CHAT_HISTORY_READ_CIRCUIT_LAST_ERROR
        _CHAT_HISTORY_READ_CIRCUIT_UNTIL_MONOTONIC = max(
            _CHAT_HISTORY_READ_CIRCUIT_UNTIL_MONOTONIC, opened_until
        )
        _CHAT_HISTORY_READ_CIRCUIT_LAST_ERROR = str(exc)
    logger.warning(
        "Opening chat history read circuit for %.1fs after %s failure: %s",
        cooldown_seconds,
        op_name,
        exc,
    )


def _record_chat_history_read_success() -> None:
    if _current_chat_history_read_circuit_remaining_seconds() <= 0:
        return
    with _CHAT_HISTORY_READ_CIRCUIT_LOCK:
        global _CHAT_HISTORY_READ_CIRCUIT_UNTIL_MONOTONIC
        global _CHAT_HISTORY_READ_CIRCUIT_LAST_ERROR
        _CHAT_HISTORY_READ_CIRCUIT_UNTIL_MONOTONIC = 0.0
        _CHAT_HISTORY_READ_CIRCUIT_LAST_ERROR = None


def _read_find_one(
    chat_history_coll,
    query: Dict[str, Any],
    projection: Optional[Dict[str, Any]] = None,
    *,
    operation: str = "find_one",
    detail: Optional[str] = None,
):
    max_time_ms = _chat_history_read_max_time_ms()
    kwargs: Dict[str, Any] = {}
    if max_time_ms > 0:
        kwargs["max_time_ms"] = max_time_ms
    comment = build_mongo_operation_comment(
        service="chat_history_service",
        collection=chat_history_collection_name,
        operation=operation,
        detail=detail,
    )
    if comment is not None:
        kwargs["comment"] = comment
    started_at = time.perf_counter()
    success = False
    error_type: Optional[str] = None
    try:
        result = chat_history_coll.find_one(query, projection, **kwargs)
        success = True
        return result
    except TypeError as exc:
        # Test doubles may not accept max_time_ms kwargs.
        error_type = type(exc).__name__
        try:
            result = chat_history_coll.find_one(query, projection)
            success = True
            return result
        except Exception as fallback_exc:
            error_type = type(fallback_exc).__name__
            raise
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        observe_mongo_operation(
            service="chat_history_service",
            collection=chat_history_collection_name,
            operation=operation,
            started_at=started_at,
            success=success,
            detail=detail,
            error_type=error_type,
        )


def _read_find(
    chat_history_coll,
    query: Dict[str, Any],
    projection: Optional[Dict[str, Any]] = None,
    *,
    operation: str = "find",
    detail: Optional[str] = None,
):
    comment = build_mongo_operation_comment(
        service="chat_history_service",
        collection=chat_history_collection_name,
        operation=operation,
        detail=detail,
    )
    started_at = time.perf_counter()
    success = False
    error_type: Optional[str] = None
    try:
        if comment is not None:
            cursor = chat_history_coll.find(query, projection, comment=comment)
        else:
            cursor = chat_history_coll.find(query, projection)
        success = True
    except TypeError as exc:
        # Test doubles may not accept PyMongo comment kwargs.
        error_type = type(exc).__name__
        try:
            cursor = chat_history_coll.find(query, projection)
            success = True
        except Exception as fallback_exc:
            error_type = type(fallback_exc).__name__
            raise
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        observe_mongo_operation(
            service="chat_history_service",
            collection=chat_history_collection_name,
            operation=operation,
            started_at=started_at,
            success=success,
            detail=detail,
            error_type=error_type,
        )
    max_time_ms = _chat_history_read_max_time_ms()
    if max_time_ms > 0:
        try:
            cursor = cursor.max_time_ms(max_time_ms)
        except Exception:
            # Some fake cursor implementations in tests may not support max_time_ms.
            pass
    return cursor


def _read_aggregate(
    chat_history_coll,
    pipeline: List[Dict[str, Any]],
    *,
    operation: str = "aggregate",
    detail: Optional[str] = None,
):
    max_time_ms = _chat_history_read_max_time_ms()
    kwargs: Dict[str, Any] = {}
    if max_time_ms > 0:
        kwargs["maxTimeMS"] = max_time_ms
    comment = build_mongo_operation_comment(
        service="chat_history_service",
        collection=chat_history_collection_name,
        operation=operation,
        detail=detail,
    )
    if comment is not None:
        kwargs["comment"] = comment
    started_at = time.perf_counter()
    success = False
    error_type: Optional[str] = None
    try:
        cursor = chat_history_coll.aggregate(pipeline, **kwargs)
        success = True
        return cursor
    except TypeError as exc:
        # Test doubles may not accept maxTimeMS kwargs.
        error_type = type(exc).__name__
        try:
            cursor = chat_history_coll.aggregate(pipeline)
            success = True
            return cursor
        except Exception as fallback_exc:
            error_type = type(fallback_exc).__name__
            raise
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        observe_mongo_operation(
            service="chat_history_service",
            collection=chat_history_collection_name,
            operation=operation,
            started_at=started_at,
            success=success,
            detail=detail,
            error_type=error_type,
        )


def _history_array_expr(field_name: str = "history") -> Dict[str, Any]:
    return {"$ifNull": [f"${field_name}", []]}


def _history_without_debug_payload_expr(history_expr: Dict[str, Any]) -> Dict[str, Any]:
    """Return an aggregation expression that removes embedded debug payloads."""

    return {
        "$map": {
            "input": history_expr,
            "as": "entry",
            "in": {
                "$cond": [
                    {"$eq": [{"$type": "$$entry"}, "object"]},
                    {
                        "$arrayToObject": {
                            "$filter": {
                                "input": {"$objectToArray": "$$entry"},
                                "as": "field",
                                "cond": {"$ne": ["$$field.k", "llm_debug_data"]},
                            }
                        }
                    },
                    "$$entry",
                ]
            },
        }
    }


def _history_tail_projection_expr(
    *,
    history_tail_limit: int,
    include_debug: bool,
) -> Dict[str, Any]:
    history_expr: Dict[str, Any] = {
        "$slice": [
            _history_array_expr(),
            -history_tail_limit,
        ]
    }
    if include_debug:
        return history_expr
    return _history_without_debug_payload_expr(history_expr)


def _ensure_chat_history_indexes(collection) -> None:
    global _CHAT_HISTORY_INDEXES_READY
    if _CHAT_HISTORY_INDEXES_READY:
        return

    with _CHAT_HISTORY_INDEXES_LOCK:
        if _CHAT_HISTORY_INDEXES_READY:
            return
        try:
            existing_indexes = [idx.get("name") for idx in collection.list_indexes()]
            if "user_id_1_session_id_1" not in existing_indexes:
                collection.create_index(
                    [("user_id", ASCENDING), ("session_id", ASCENDING)],
                    name="user_id_1_session_id_1",
                )
            if "session_id_1_created_at_1" not in existing_indexes:
                collection.create_index(
                    [("session_id", ASCENDING), ("created_at", ASCENDING)],
                    name="session_id_1_created_at_1",
                )
            if "user_id_1_session_id_1_namespace_1" not in existing_indexes:
                collection.create_index(
                    [
                        ("user_id", ASCENDING),
                        ("session_id", ASCENDING),
                        ("namespace", ASCENDING),
                    ],
                    name="user_id_1_session_id_1_namespace_1",
                )
            if "user_id_1_updated_at_-1" not in existing_indexes:
                collection.create_index(
                    [("user_id", ASCENDING), ("updated_at", DESCENDING)],
                    name="user_id_1_updated_at_-1",
                )
            if "namespace_1_user_id_1_updated_at_-1" not in existing_indexes:
                collection.create_index(
                    [
                        ("namespace", ASCENDING),
                        ("user_id", ASCENDING),
                        ("updated_at", DESCENDING),
                    ],
                    name="namespace_1_user_id_1_updated_at_-1",
                )
            if "namespace_1_user_id_1_updated_at_-1_created_at_-1" not in existing_indexes:
                collection.create_index(
                    [
                        ("namespace", ASCENDING),
                        ("user_id", ASCENDING),
                        ("updated_at", DESCENDING),
                        ("created_at", DESCENDING),
                    ],
                    name="namespace_1_user_id_1_updated_at_-1_created_at_-1",
                )
            if _CHAT_HISTORY_LIGHT_SESSION_METADATA_INDEX_NAME not in existing_indexes:
                collection.create_index(
                    [
                        ("namespace", ASCENDING),
                        ("user_id", ASCENDING),
                        ("updated_at", DESCENDING),
                        ("created_at", DESCENDING),
                        ("session_id", ASCENDING),
                        ("session_name", ASCENDING),
                        ("organisation_concept_id", ASCENDING),
                        ("origin_kind", ASCENDING),
                        ("created_by_actor_concept_id", ASCENDING),
                        ("created_by_actor_type", ASCENDING),
                        ("is_agent_created", ASCENDING),
                        ("test_artifact_kind", ASCENDING),
                    ],
                    name=_CHAT_HISTORY_LIGHT_SESSION_METADATA_INDEX_NAME,
                )
        except Exception as exc:
            logger.warning(
                "Index creation skipped for chat_history collection: %s", exc
            )
        _CHAT_HISTORY_INDEXES_READY = True


def _normalise_session_name(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name:
        return None
    if len(name) > _SESSION_NAME_MAX_LEN:
        name = name[: _SESSION_NAME_MAX_LEN - 3].rstrip() + "..."
    return name


def _normalise_session_provenance_text(
    value: Any,
    *,
    max_len: int = 160,
    identifier: bool = False,
) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if identifier:
        cleaned = cleaned.lower().replace("-", "_").replace(" ", "_")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned or None


def _normalise_session_agent_created_flag(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"1", "true", "yes", "on", "y"}:
            return True
        if cleaned in {"0", "false", "no", "off", "n"}:
            return False
    return None


def _normalise_chat_session_provenance_fields(
    *,
    origin_kind: Any = None,
    created_by_actor_concept_id: Any = None,
    created_by_actor_type: Any = None,
    is_agent_created: Any = None,
    test_artifact_kind: Any = None,
) -> Dict[str, Any]:
    """Normalise durable conversation-origin fields for session metadata."""

    normalised_origin = _normalise_session_provenance_text(
        origin_kind,
        max_len=80,
        identifier=True,
    )
    normalised_actor_id = _normalise_session_provenance_text(
        created_by_actor_concept_id,
        max_len=160,
    )
    normalised_actor_type = _normalise_session_provenance_text(
        created_by_actor_type,
        max_len=160,
    )
    normalised_test_kind = _normalise_session_provenance_text(
        test_artifact_kind,
        max_len=100,
        identifier=True,
    )
    explicit_agent_flag = _normalise_session_agent_created_flag(is_agent_created)

    inferred_agent_created = bool(
        normalised_origin in CHAT_SESSION_AGENT_CREATED_ORIGIN_KINDS
        or normalised_test_kind
    )
    agent_created = (
        explicit_agent_flag
        if explicit_agent_flag is not None
        else inferred_agent_created
    )

    fields: Dict[str, Any] = {}
    if normalised_origin:
        fields["origin_kind"] = normalised_origin
    if normalised_actor_id:
        fields["created_by_actor_concept_id"] = normalised_actor_id
    if normalised_actor_type:
        fields["created_by_actor_type"] = normalised_actor_type
    if normalised_test_kind:
        fields["test_artifact_kind"] = normalised_test_kind
    if agent_created:
        fields["is_agent_created"] = True
    return fields


def _session_provenance_from_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    origin_kind = _normalise_session_provenance_text(
        doc.get("origin_kind"),
        max_len=80,
        identifier=True,
    )
    actor_id = _normalise_session_provenance_text(
        doc.get("created_by_actor_concept_id"),
        max_len=160,
    )
    actor_type = _normalise_session_provenance_text(
        doc.get("created_by_actor_type"),
        max_len=160,
    )
    test_kind = _normalise_session_provenance_text(
        doc.get("test_artifact_kind"),
        max_len=100,
        identifier=True,
    )
    agent_flag = _normalise_session_agent_created_flag(doc.get("is_agent_created"))
    is_agent_created = bool(
        agent_flag is True
        or origin_kind in CHAT_SESSION_AGENT_CREATED_ORIGIN_KINDS
        or test_kind
    )
    return {
        "origin_kind": origin_kind,
        "created_by_actor_concept_id": actor_id,
        "created_by_actor_type": actor_type,
        "is_agent_created": is_agent_created,
        "test_artifact_kind": test_kind,
    }


def _normalise_chat_session_agent_visibility(value: Any) -> str:
    if value is None:
        return CHAT_SESSION_AGENT_VISIBILITY_INCLUDE
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if not cleaned:
            return CHAT_SESSION_AGENT_VISIBILITY_INCLUDE
        if cleaned in VALID_CHAT_SESSION_AGENT_VISIBILITY_VALUES:
            return cleaned
    raise ChatHistoryServiceError(
        "agent_visibility must be one of: "
        + ", ".join(sorted(VALID_CHAT_SESSION_AGENT_VISIBILITY_VALUES))
    )


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"1", "true", "yes", "on", "y"}:
            return True
        if cleaned in {"0", "false", "no", "off", "n"}:
            return False
    return default


def _session_summary_is_agent_created(summary: Dict[str, Any]) -> bool:
    if not isinstance(summary, dict):
        return False
    if _normalise_session_agent_created_flag(summary.get("is_agent_created")) is True:
        return True
    origin_kind = _normalise_session_provenance_text(
        summary.get("origin_kind"),
        max_len=80,
        identifier=True,
    )
    test_kind = _normalise_session_provenance_text(
        summary.get("test_artifact_kind"),
        max_len=100,
        identifier=True,
    )
    return bool(origin_kind in CHAT_SESSION_AGENT_CREATED_ORIGIN_KINDS or test_kind)


def _session_summary_recency(summary: Dict[str, Any]) -> datetime:
    for key in ("last_message_at", "created_at", "shared_accepted_at"):
        value = summary.get(key)
        parsed = _coerce_datetime(value)
        if parsed is not None:
            return parsed
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def apply_chat_session_agent_visibility(
    sessions: Iterable[Dict[str, Any]],
    *,
    agent_visibility: Any = CHAT_SESSION_AGENT_VISIBILITY_INCLUDE,
    keep_newest_agent_created: Any = True,
    limit: int | None = None,
) -> Dict[str, Any]:
    """Apply test/agent-created conversation visibility before count limiting."""

    visibility = _normalise_chat_session_agent_visibility(agent_visibility)
    keep_newest = _coerce_bool(keep_newest_agent_created, default=True)
    session_list = [session for session in sessions if isinstance(session, dict)]
    agent_sessions = [
        session for session in session_list if _session_summary_is_agent_created(session)
    ]
    newest_agent_session = None
    if keep_newest and agent_sessions:
        newest_agent_session = max(agent_sessions, key=_session_summary_recency)
    newest_agent_session_id = (
        newest_agent_session.get("session_id") if newest_agent_session else None
    )

    hidden_agent_session_ids: set[str] = set()
    filtered: List[Dict[str, Any]] = []
    for session_summary in session_list:
        session_id = session_summary.get("session_id")
        is_agent_session = _session_summary_is_agent_created(session_summary)
        if visibility == CHAT_SESSION_AGENT_VISIBILITY_ONLY:
            if is_agent_session:
                filtered.append(session_summary)
            continue
        if visibility == CHAT_SESSION_AGENT_VISIBILITY_EXCLUDE and is_agent_session:
            if keep_newest and session_id and session_id == newest_agent_session_id:
                filtered.append(session_summary)
            else:
                if isinstance(session_id, str) and session_id:
                    hidden_agent_session_ids.add(session_id)
            continue
        filtered.append(session_summary)

    total_after_visibility = len(filtered)
    safe_limit = None
    if limit is not None:
        try:
            safe_limit = max(1, int(limit))
        except (TypeError, ValueError):
            safe_limit = None
    limited = filtered[:safe_limit] if safe_limit is not None else filtered
    visible_ids: set[str] = set()
    for session in limited:
        session_id = session.get("session_id")
        if isinstance(session_id, str) and session_id:
            visible_ids.add(session_id)
    hidden_by_limit_count = max(0, total_after_visibility - len(limited))

    return {
        "sessions": limited,
        "agent_visibility": visibility,
        "keep_newest_agent_created": keep_newest,
        "agent_created_session_total": len(agent_sessions),
        "hidden_agent_created_session_count": len(hidden_agent_session_ids),
        "hidden_agent_created_session_ids": sorted(hidden_agent_session_ids)[:200],
        "newest_visible_agent_created_session_id": newest_agent_session_id,
        "total_after_agent_visibility": total_after_visibility,
        "hidden_by_limit_count": hidden_by_limit_count,
        "limit": safe_limit,
        "agent_visibility_applied": True,
        "visible_session_ids": sorted(visible_ids),
    }


def _add_chat_session_provenance_projection(
    projection: Dict[str, Any],
) -> Dict[str, Any]:
    for field_name in CHAT_SESSION_PROVENANCE_FIELDS:
        projection[field_name] = 1
    return projection


def _default_session_name(now: Optional[datetime] = None) -> str:
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone()
    return timestamp.strftime("Chat %Y-%m-%d %H:%M")


def _session_name_from_message(content: Any) -> Optional[str]:
    if not isinstance(content, str):
        return None
    first_line = content.splitlines()[0].strip()
    return _normalise_session_name(first_line)


def _derive_rag_namespace(
    *, session_context: Dict[str, Any], user_id: str
) -> Optional[str]:
    ns = session_context.get("namespace")
    if isinstance(ns, str) and ns.strip():
        return ns.strip()

    org_id = session_context.get("organisation_concept_id") or session_context.get(
        "org_id"
    )
    if isinstance(org_id, str) and org_id.strip():
        try:
            from src.backend.services.namespace_service import derive_namespace

            user_slug = user_id[3:] if user_id.startswith("#V#") else user_id
            org_slug = org_id[3:] if org_id.startswith("#V#") else org_id
            return derive_namespace(user_slug, org_slug)
        except Exception:
            return None

    return None


def _build_rag_metadata(
    *,
    session_context: Dict[str, Any],
    user_id: str,
    session_id: str,
    role: str,
    channel: Optional[str] = None,
    history_index: Optional[int] = None,
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "user_id": user_id,
        "session_id": session_id,
        "role": role,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "chat_message",
    }
    if isinstance(channel, str) and channel.strip():
        metadata["channel"] = channel.strip()
    if isinstance(history_index, int) and history_index >= 0:
        metadata["history_index"] = history_index
    if isinstance(generated_at, datetime):
        metadata["generated_at"] = generated_at.isoformat()

    org_concept_id = session_context.get("organisation_concept_id")
    if org_concept_id:
        from ..utils.concept_id_utils import ensure_v_concept_prefix

        metadata["organisation_concept_id"] = (
            ensure_v_concept_prefix(org_concept_id) or org_concept_id
        )
    if session_context.get("role_in_org"):
        metadata["role_in_org"] = session_context["role_in_org"]

    return metadata


def _truncate_for_rag(text: str, *, max_chars: int = 5000) -> str:
    if not isinstance(text, str):
        return ""
    txt = text
    if len(txt) > max_chars:
        txt = txt[:max_chars]
    return txt


def _safe_str(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _extract_prompt_text_from_llm_debug_data(
    llm_debug_data: Optional[Dict[str, Any]],
) -> Optional[str]:
    if not isinstance(llm_debug_data, dict):
        return None

    direct_prompt = _safe_str(llm_debug_data.get("prompt_text"))
    if direct_prompt:
        return direct_prompt

    messages = llm_debug_data.get("messages")
    if isinstance(messages, list):
        for item in reversed(messages):
            if not isinstance(item, dict):
                continue
            if item.get("role") != "user":
                continue
            content = _safe_str(item.get("content"))
            if content:
                return content

    user_prompt = llm_debug_data.get("user_prompt")
    if isinstance(user_prompt, dict):
        for key in ("prompt_text", "content", "preview"):
            candidate = _safe_str(user_prompt.get(key))
            if candidate:
                return candidate

    return None


def _ensure_turn_execution_record_for_assistant_message(
    *,
    message: Dict[str, Any],
    llm_debug_data: Optional[Dict[str, Any]],
    user_id: str,
    session_id: str,
    namespace: Optional[str],
    org_id: Optional[str],
    interaction_timestamp_utc: Optional[str],
) -> Optional[Dict[str, Any]]:
    if message.get("role") != "assistant":
        return llm_debug_data
    if not isinstance(llm_debug_data, dict):
        return llm_debug_data
    existing = llm_debug_data.get("turn_execution_record")
    if isinstance(existing, dict):
        return llm_debug_data

    request_id = _safe_str(llm_debug_data.get("request_id"))
    if not request_id:
        return llm_debug_data

    try:
        rebuilt = build_turn_execution_record(
            request_id=request_id,
            session_id=session_id,
            namespace=namespace,
            actor_concept_id=(
                llm_debug_data.get("actor_concept_id")
                if isinstance(llm_debug_data.get("actor_concept_id"), str)
                and llm_debug_data.get("actor_concept_id", "").strip()
                else None
            ),
            user_id=user_id,
            org_id=org_id,
            prompt_text=_extract_prompt_text_from_llm_debug_data(llm_debug_data),
            response_text=_safe_str(message.get("content")),
            interaction_timestamp_utc=interaction_timestamp_utc,
            workflow_discovery=(
                llm_debug_data.get("workflow_discovery")
                if isinstance(llm_debug_data.get("workflow_discovery"), dict)
                else None
            ),
            workflow_routing=infer_turn_execution_workflow_routing_from_debug(
                llm_debug=llm_debug_data
            ),
            tool_invocations=(
                llm_debug_data.get("tool_invocations")
                if isinstance(llm_debug_data.get("tool_invocations"), list)
                else []
            ),
            search_evidence=(
                llm_debug_data.get("search_evidence")
                if isinstance(llm_debug_data.get("search_evidence"), list)
                else []
            ),
            turn_execution_diagnostics=(
                llm_debug_data.get("turn_execution_diagnostics")
                if isinstance(llm_debug_data.get("turn_execution_diagnostics"), dict)
                else None
            ),
            aux_llm_calls=(
                llm_debug_data.get("aux_llm_calls")
                if isinstance(llm_debug_data.get("aux_llm_calls"), list)
                else []
            ),
        )
        if isinstance(rebuilt, dict):
            rebuilt["reconstruction"] = {
                "source": "chat_history_service.add_message_to_history",
                "method": "build_turn_execution_record",
            }
            llm_debug_data["turn_execution_record"] = rebuilt
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "Could not synthesise turn_execution_record for request_id=%s: %s",
            request_id,
            exc,
        )

    return llm_debug_data


def get_session_context() -> Dict[str, Any]:
    """
    Get organisation and role context from Flask session.
    Returns dict with org_id and role keys (may be None if not in context).
    """
    try:
        from flask import session as flask_session

        return {
            # Prefer current key, fall back to legacy.
            "organisation_concept_id": flask_session.get("organisation_concept_id")
            or flask_session.get("org_id"),
            "role_in_org": flask_session.get("role_in_org"),
            "namespace": flask_session.get("namespace"),
            # Compatibility alias: some call sites historically looked up org_id.
            "org_id": flask_session.get("organisation_concept_id")
            or flask_session.get("org_id"),
        }
    except (ImportError, RuntimeError):
        # Not in Flask context or session not available
        return {
            "organisation_concept_id": None,
            "role_in_org": None,
            "namespace": None,
            "org_id": None,
        }


def resolve_chat_history_namespace(user_id: str) -> Optional[str]:
    if not isinstance(user_id, str) or not user_id:
        return None
    session_context = get_session_context()
    return _derive_rag_namespace(session_context=session_context, user_id=user_id)


def build_chat_history_query(
    *,
    user_id: str,
    session_id: Optional[str] = None,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
) -> Dict[str, Any]:
    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required.")

    query: Dict[str, Any] = {"user_id": user_id}
    if isinstance(session_id, str) and session_id.strip():
        query["session_id"] = session_id.strip()

    if isinstance(namespace, str) and namespace.strip():
        ns = namespace.strip()
        if include_legacy:
            query["$or"] = _legacy_namespace_match_clauses(ns)
        else:
            query["namespace"] = ns

    return query


def _legacy_namespace_match_clauses(namespace: str) -> List[Dict[str, Any]]:
    return [
        {"namespace": namespace},
        {"namespace": {"$exists": False}},
        {"namespace": {"$eq": None}},
        {"namespace": {"$in": ["", " "]}},
    ]


def _build_chat_history_session_read_queries(
    *,
    user_id: str,
    session_id: str,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
) -> List[Dict[str, Any]]:
    session_id_value = _safe_str(session_id)
    if not session_id_value:
        raise ChatHistoryServiceError("session_id is required.")

    namespace_value = _safe_str(namespace)
    base_query: Dict[str, Any] = {
        "user_id": user_id,
        "session_id": session_id_value,
    }
    if not namespace_value:
        return [base_query]

    queries: List[Dict[str, Any]] = [
        {
            **base_query,
            "namespace": namespace_value,
        }
    ]
    if include_legacy:
        queries.append(
            {
                **base_query,
                "$or": _legacy_namespace_match_clauses(namespace_value),
            }
        )
    return queries


class ChatHistoryServiceError(Exception):
    """Exception raised for chat history service errors."""

    pass


def get_chat_history_collection_service(*, read_only: bool = False):
    """Get the chat_history collection."""
    db = get_db()
    if db is None:
        return None
    coll = db[chat_history_collection_name]
    _ensure_chat_history_indexes(coll)
    if read_only and _chat_history_use_primary_preferred_reads():
        try:
            coll = coll.with_options(read_preference=ReadPreference.PRIMARY_PREFERRED)
        except Exception:
            # Safety: if read-preference tuning is unavailable, fall back to default.
            pass
    return coll


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        # Normalise to tz-aware
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        txt = value.strip()
        if not txt:
            return None
        # Support common ISO strings ending with Z
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(txt)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _is_reset_marker(entry: Dict[str, Any]) -> bool:
    return entry.get("role") == "system" and entry.get("content") == "__RESET__"


def _iter_non_reset_messages(
    history: Iterable[Dict[str, Any]],
) -> Iterable[Dict[str, Any]]:
    for msg in history:
        if isinstance(msg, dict) and not _is_reset_marker(msg):
            yield msg


def _compute_rag_history_signature(
    history: List[Dict[str, Any]],
) -> tuple[int, str]:
    indexable_total = 0
    last_index: Optional[int] = None
    last_ts_iso = ""
    last_content_hash = ""

    for idx, msg in enumerate(history):
        if not isinstance(msg, dict) or _is_reset_marker(msg):
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        indexable_total += 1
        last_index = idx
        last_ts = _coerce_datetime(msg.get("timestamp"))
        if last_ts:
            last_ts_iso = last_ts.isoformat()
        last_content_hash = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()

    if indexable_total == 0:
        return 0, ""

    signature = f"{indexable_total}|{last_index}|{last_ts_iso}|{last_content_hash}"
    return indexable_total, signature


def _should_skip_rag_reindex(
    doc: Dict[str, Any],
    signature: str,
    indexable_total: int,
) -> bool:
    if not signature:
        return False
    if doc.get("rag_history_signature") != signature:
        return False

    try:
        rag_failed = int(doc.get("rag_indexed_failed") or 0)
    except (TypeError, ValueError):
        rag_failed = 0
    if rag_failed > 0:
        return False

    try:
        rag_success = int(doc.get("rag_indexed_success") or 0)
    except (TypeError, ValueError):
        rag_success = 0
    if rag_success < indexable_total:
        return False

    return True


def _update_rag_history_signature(
    collection,
    doc_id: Any,
    signature: str,
    indexable_total: int,
) -> None:
    if not signature:
        return
    try:
        collection.update_one(
            {"_id": doc_id},
            {
                "$set": {
                    "rag_history_signature": signature,
                    "rag_history_indexable_total": indexable_total,
                }
            },
        )
    except Exception:
        pass


def _infer_last_message_timestamp(doc: Dict[str, Any]) -> Optional[datetime]:
    history = doc.get("history") or []
    last_ts: Optional[datetime] = None
    for msg in _iter_non_reset_messages(history):
        ts = _coerce_datetime(msg.get("timestamp"))
        if ts is None:
            continue
        if last_ts is None or ts > last_ts:
            last_ts = ts
    if last_ts is not None:
        return last_ts
    return _coerce_datetime(doc.get("updated_at")) or _coerce_datetime(
        doc.get("created_at")
    )


def _infer_created_timestamp(doc: Dict[str, Any]) -> Optional[datetime]:
    return _coerce_datetime(doc.get("created_at")) or _coerce_datetime(
        doc.get("updated_at")
    )


def _split_history_into_segments(
    history: List[Dict[str, Any]],
) -> List[List[Dict[str, Any]]]:
    segments: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        if _is_reset_marker(entry):
            if current:
                segments.append(current)
            current = []
            continue
        current.append(entry)
    if current:
        segments.append(current)
    return segments


def _split_history_into_segments_with_locations(
    history: List[Dict[str, Any]],
    *,
    session_id: str,
    include_debug: bool = True,
    history_offset: int = 0,
    owner_user_id: Optional[str] = None,
) -> List[List[Dict[str, Any]]]:
    """Split history into segments, attaching stable location metadata.

    The `history_index` refers to the index in the raw stored `history` array,
    including reset markers. This allows later targeted updates via
    `history.<index>` without ambiguity.
    """

    segments: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []

    if not isinstance(session_id, str) or not session_id:
        session_id = ""

    for idx, entry in enumerate(history):
        if not isinstance(entry, dict):
            continue
        if _is_reset_marker(entry):
            if current:
                segments.append(current)
            current = []
            continue

        absolute_index = idx + history_offset
        copied = {
            "role": entry.get("role"),
            "content": entry.get("content"),
            "timestamp": entry.get("timestamp"),
        }
        author_user_id = entry.get("author_user_id")
        if not isinstance(author_user_id, str) or not author_user_id.strip():
            author_user_id = None
        if not author_user_id and entry.get("role") == "user":
            if isinstance(owner_user_id, str) and owner_user_id.strip():
                author_user_id = owner_user_id
        if author_user_id:
            copied["author_user_id"] = author_user_id
        if include_debug and "llm_debug_data" in entry:
            copied["llm_debug_data"] = entry.get("llm_debug_data")
        existing_location = entry.get("history_location")
        if isinstance(existing_location, dict):
            existing_index = existing_location.get("history_index")
            existing_session = existing_location.get("session_id") or session_id
            if isinstance(existing_index, int) and existing_index >= 0:
                if history_offset > 0 and existing_index < history_offset:
                    existing_index = existing_index + history_offset
                copied["history_location"] = {
                    "session_id": existing_session,
                    "history_index": existing_index,
                }
            else:
                copied["history_location"] = {
                    "session_id": session_id,
                    "history_index": absolute_index,
                }
        else:
            copied["history_location"] = {
                "session_id": session_id,
                "history_index": absolute_index,
            }
        current.append(copied)

    if current:
        segments.append(current)

    return segments


def _chunk_history_segments(
    segments: List[List[Dict[str, Any]]],
    segment_size: Optional[int],
) -> List[List[Dict[str, Any]]]:
    if not segment_size or segment_size <= 0:
        return segments

    chunked: List[List[Dict[str, Any]]] = []
    for segment in segments:
        if not isinstance(segment, list) or not segment:
            continue
        if len(segment) <= segment_size:
            chunked.append(segment)
            continue
        for idx in range(0, len(segment), segment_size):
            chunk = segment[idx : idx + segment_size]
            if chunk:
                chunked.append(chunk)
    return chunked


def get_chat_history(
    user_id: str,
    session_id: str,
    *,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    hydrate_blob_refs: bool = False,
) -> List[Dict[str, Any]]:
    """
    Retrieves the chat history for a specific user and session.

    Args:
        user_id: The user's concept ID (e.g., "#V#michael_witbrock")
        session_id: The session UUID

    Returns:
        List of message dictionaries with role and content
    """
    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")

    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    _guard_chat_history_read("get_chat_history")

    try:
        doc = None
        for query in _build_chat_history_session_read_queries(
            user_id=user_id,
            session_id=session_id,
            namespace=namespace,
            include_legacy=include_legacy,
        ):
            doc = _read_find_one(
                chat_history_coll,
                query,
                operation="get_chat_history.find_session",
            )
            if doc is not None:
                break
        _record_chat_history_read_success()

        if doc:
            return _normalise_chat_history_entries(
                doc.get("history", []),
                hydrate_blob_refs=hydrate_blob_refs,
            )
        return []
    except PyMongoError as e:
        _record_chat_history_read_failure("get_chat_history", e)
        logger.error(f"Error retrieving chat history: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not retrieve chat history: {e}") from e


def _normalise_chat_history_entries(
    history: Any,
    *,
    hydrate_blob_refs: bool = False,
) -> List[Dict[str, Any]]:
    if not isinstance(history, list):
        return []

    entries: List[Dict[str, Any]] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        if hydrate_blob_refs:
            hydrated = hydrate_debug_payload_blob_refs(entry, fail_soft=True)
            payload = hydrated.payload
            if isinstance(payload, dict):
                entries.append(payload)
            else:
                entries.append(dict(entry))
        else:
            entries.append(dict(entry))
    return entries


def _hydrate_chat_history_entries(history: Any) -> List[Dict[str, Any]]:
    return _normalise_chat_history_entries(history, hydrate_blob_refs=True)


def get_chat_history_segments(
    user_id: str,
    session_id: str,
    *,
    include_locations: bool = False,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    segment_size: Optional[int] = None,
    include_debug: bool = True,
    history_tail_limit: Optional[int] = None,
    return_meta: bool = False,
    hydrate_blob_refs: bool = False,
) -> List[List[Dict[str, Any]]] | tuple[List[List[Dict[str, Any]]], Dict[str, Any]]:
    """
    Return chat history split into segments separated by reset markers.
    Retrieves history from the requested session only.

    When return_meta is True, returns (segments, {"history_truncated": bool}).
    """
    if not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if not session_id:
        raise ChatHistoryServiceError("session_id is required.")

    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    _guard_chat_history_read("get_chat_history_segments")

    try:
        history_offset = 0
        history_length = None
        doc = None
        projection = None
        read_queries = _build_chat_history_session_read_queries(
            user_id=user_id,
            session_id=session_id,
            namespace=namespace,
            include_legacy=include_legacy,
        )
        for query in read_queries:
            if isinstance(history_tail_limit, int) and history_tail_limit > 0:
                if hasattr(chat_history_coll, "aggregate") and callable(
                    getattr(chat_history_coll, "aggregate")
                ):
                    pipeline = [
                        {"$match": query},
                        {
                            "$project": {
                                "history": _history_tail_projection_expr(
                                    history_tail_limit=history_tail_limit,
                                    include_debug=include_debug,
                                ),
                                "history_length": {"$size": _history_array_expr()},
                            }
                        },
                    ]
                    try:
                        doc = next(
                            _read_aggregate(
                                chat_history_coll,
                                pipeline,
                                operation="get_chat_history_segments.aggregate_tail",
                            ),
                            None,
                        )
                    except PyMongoError:
                        raise
                    except Exception:
                        doc = None
                if doc is None:
                    doc = _read_find_one(
                        chat_history_coll,
                        query,
                        projection,
                        operation="get_chat_history_segments.tail_fallback",
                    )
                    if doc is not None:
                        full_history = doc.get("history") or []
                        if isinstance(full_history, list):
                            history_length = len(full_history)
                            doc = dict(doc)
                            doc["history"] = full_history[-history_tail_limit:]
            else:
                doc = _read_find_one(
                    chat_history_coll,
                    query,
                    projection,
                    operation="get_chat_history_segments.find_session",
                )
            if doc is not None:
                break
        if not doc:
            _record_chat_history_read_success()
            return ([], {"history_truncated": False}) if return_meta else []

        history = _normalise_chat_history_entries(
            doc.get("history") or [],
            hydrate_blob_refs=hydrate_blob_refs,
        )
        if not isinstance(history, list) or not history:
            _record_chat_history_read_success()
            return ([], {"history_truncated": False}) if return_meta else []
        if history_length is None:
            history_length_raw = doc.get("history_length")
            if isinstance(history_length_raw, int):
                history_length = history_length_raw

        if isinstance(history_length, int) and history_length >= 0:
            history_offset = max(0, history_length - len(history))
            history_truncated = history_length > len(history)
        else:
            history_truncated = bool(
                isinstance(history_tail_limit, int)
                and history_tail_limit > 0
                and len(history) >= history_tail_limit
            )

        if include_locations:
            segments = _split_history_into_segments_with_locations(
                history,
                session_id=session_id,
                include_debug=include_debug,
                history_offset=history_offset,
                owner_user_id=user_id,
            )
        else:
            segments = _split_history_into_segments(history)

        if not include_debug and segments:
            stripped: List[List[Dict[str, Any]]] = []
            for segment in segments:
                cleaned_segment: List[Dict[str, Any]] = []
                for entry in segment:
                    if not isinstance(entry, dict):
                        continue
                    cleaned = dict(entry)
                    cleaned.pop("llm_debug_data", None)
                    cleaned_segment.append(cleaned)
                if cleaned_segment:
                    stripped.append(cleaned_segment)
            segments = stripped

        result = _chunk_history_segments(segments, segment_size)
        _record_chat_history_read_success()
        if return_meta:
            return result, {"history_truncated": history_truncated}
        return result
    except PyMongoError as e:
        _record_chat_history_read_failure("get_chat_history_segments", e)
        logger.error(f"Error retrieving segmented chat history: {e}", exc_info=True)
        raise ChatHistoryServiceError(
            f"Could not retrieve segmented chat history: {e}"
        ) from e


def get_chat_history_debug_entry(
    *,
    user_id: str,
    session_id: str,
    history_index: int,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    hydrate_blob_refs: bool = True,
) -> Optional[Dict[str, Any]]:
    """Return stored llm_debug_data for a specific history entry."""
    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id:
        raise ChatHistoryServiceError("session_id is required.")
    if not isinstance(history_index, int) or history_index < 0:
        raise ChatHistoryServiceError("history_index must be a non-negative integer.")

    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    _guard_chat_history_read("get_chat_history_debug_entry")

    try:
        projection = {"history": {"$slice": [history_index, 1]}}
        doc = None
        for query in _build_chat_history_session_read_queries(
            user_id=user_id,
            session_id=session_id,
            namespace=namespace,
            include_legacy=include_legacy,
        ):
            doc = _read_find_one(
                chat_history_coll,
                query,
                projection,
                operation="get_chat_history_debug_entry.slice_entry",
            )
            if doc is not None:
                break
        if not doc:
            _record_chat_history_read_success()
            return None
        history = doc.get("history") or []
        if not isinstance(history, list) or not history:
            _record_chat_history_read_success()
            return None
        entry = history[0]
        if not isinstance(entry, dict):
            _record_chat_history_read_success()
            return None
        debug_data = entry.get("llm_debug_data")
        if not isinstance(debug_data, dict):
            _record_chat_history_read_success()
            return None
        if hydrate_blob_refs:
            hydrated = hydrate_debug_payload_blob_refs(debug_data, fail_soft=True)
            debug_data = (
                hydrated.payload
                if isinstance(hydrated.payload, dict)
                else debug_data
            )
        _record_chat_history_read_success()
        return debug_data
    except PyMongoError as e:
        _record_chat_history_read_failure("get_chat_history_debug_entry", e)
        logger.error(
            "Error retrieving llm_debug_data at history index %s: %s",
            history_index,
            e,
            exc_info=True,
        )
        raise ChatHistoryServiceError(
            f"Could not retrieve llm_debug_data at history index {history_index}: {e}"
        ) from e


def upsert_presenter_channels_for_history_message(
    *,
    user_id: str,
    session_id: str,
    history_index: int,
    presenter_channels: Dict[str, Any],
    display_elements: Optional[Dict[str, Any]] = None,
    turn_output_health: Optional[Dict[str, Any]] = None,
    generated_at: Optional[datetime] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Persist presenter channels onto a stored history message.

    Safety properties:
    - Does NOT touch the session document's `updated_at` (avoids breaking recency ordering).
    - By default, does not overwrite an existing `presenter_channels.spoken`.

    Returns a dict describing whether an update occurred.
    """

    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required")
    if not isinstance(session_id, str) or not session_id:
        raise ChatHistoryServiceError("session_id is required")
    if not isinstance(history_index, int) or history_index < 0:
        raise ChatHistoryServiceError("history_index must be a non-negative integer")
    if not isinstance(presenter_channels, dict) or not presenter_channels:
        raise ChatHistoryServiceError("presenter_channels must be a non-empty dict")
    if display_elements is not None and not isinstance(display_elements, dict):
        raise ChatHistoryServiceError("display_elements must be a dict when provided")
    if turn_output_health is not None and not isinstance(turn_output_health, dict):
        raise ChatHistoryServiceError(
            "turn_output_health must be a dict when provided"
        )

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        doc = chat_history_coll.find_one(
            {"user_id": user_id, "session_id": session_id}, {"history": 1}
        )
        history = (doc or {}).get("history") or []
        if not isinstance(history, list) or history_index >= len(history):
            return {"updated": False, "reason": "index_out_of_range"}

        entry = history[history_index]
        if not isinstance(entry, dict):
            return {"updated": False, "reason": "entry_not_dict"}
        if entry.get("role") != "assistant":
            return {"updated": False, "reason": "not_assistant"}

        existing_debug = entry.get("llm_debug_data")
        existing_channels = None
        if isinstance(existing_debug, dict):
            existing_channels = existing_debug.get("presenter_channels")

        if not force and isinstance(existing_channels, dict):
            existing_spoken = existing_channels.get("spoken")
            if isinstance(existing_spoken, str) and existing_spoken.strip():
                return {"updated": False, "reason": "spoken_already_present"}

        if generated_at is None:
            generated_at = datetime.now(timezone.utc)

        set_fields: Dict[str, Any] = {
            f"history.{history_index}.llm_debug_data.presenter_channels": presenter_channels,
            f"history.{history_index}.llm_debug_data.presenter_channels_generated_at": generated_at,
        }
        if isinstance(display_elements, dict) and display_elements:
            set_fields[f"history.{history_index}.llm_debug_data.display_elements"] = (
                display_elements
            )
            set_fields[
                f"history.{history_index}.llm_debug_data.display_elements_generated_at"
            ] = generated_at
        if isinstance(turn_output_health, dict) and turn_output_health:
            set_fields[f"history.{history_index}.llm_debug_data.turn_output_health"] = (
                turn_output_health
            )
            set_fields[
                f"history.{history_index}.llm_debug_data.turn_output_health_generated_at"
            ] = generated_at

        result = chat_history_coll.update_one(
            {"user_id": user_id, "session_id": session_id}, {"$set": set_fields}
        )

        updated = bool(getattr(result, "modified_count", 0) > 0)

        # If we generated spoken narration for an existing stored message, index it to RAG
        # so it is retrievable later.
        if updated and get_rag_service:
            try:
                rag = get_rag_service()
                spoken = presenter_channels.get("spoken")
                if isinstance(spoken, str) and spoken.strip():
                    session_context = get_session_context()
                    ns = _derive_rag_namespace(
                        session_context=session_context, user_id=user_id
                    )
                    if not isinstance(ns, str) or not ns.strip():
                        ns = "chat_history"

                    doc_id = str(
                        uuid.uuid5(
                            _DETERMINISTIC_RAG_DOC_NAMESPACE,
                            f"{user_id}|{session_id}|{history_index}|spoken",
                        )
                    )
                    doc = {
                        "id": doc_id,
                        "text": _truncate_for_rag(spoken.strip()),
                        "metadata": _build_rag_metadata(
                            session_context=session_context,
                            user_id=user_id,
                            session_id=session_id,
                            role="assistant",
                            channel="spoken",
                            history_index=history_index,
                            generated_at=generated_at,
                        ),
                    }
                    rag.upsert_documents([doc], namespace=ns)
            except Exception as e:
                logger.warning(
                    "Failed to index backfilled spoken narration to RAG: %s", e
                )

        return {
            "updated": updated,
            "matched": bool(getattr(result, "matched_count", 0) > 0),
        }
    except PyMongoError as e:
        logger.error(
            "Error upserting presenter channels for history message: %s",
            e,
            exc_info=True,
        )
        raise ChatHistoryServiceError(
            f"Could not update history message presenter channels: {e}"
        ) from e


def update_llm_debug_data_for_request_id(
    *,
    user_id: str,
    session_id: str,
    request_id: str,
    updates: Dict[str, Any],
) -> Dict[str, Any]:
    """Update llm_debug_data fields for the assistant message matching request_id."""

    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required")
    if not isinstance(session_id, str) or not session_id:
        raise ChatHistoryServiceError("session_id is required")
    if not isinstance(request_id, str) or not request_id:
        raise ChatHistoryServiceError("request_id is required")
    if not isinstance(updates, dict) or not updates:
        return {"updated": False, "reason": "no_updates"}

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    set_fields = {
        f"history.$.llm_debug_data.{key}": value for key, value in updates.items()
    }

    try:
        result = chat_history_coll.update_one(
            {
                "user_id": user_id,
                "session_id": session_id,
                "history": {
                    "$elemMatch": {
                        "role": "assistant",
                        "llm_debug_data.request_id": request_id,
                    }
                },
            },
            {"$set": set_fields},
        )
        updated = bool(getattr(result, "modified_count", 0) > 0)
        matched = bool(getattr(result, "matched_count", 0) > 0)
        reason = None if matched else "request_id_not_found"
        return {"updated": updated, "matched": matched, "reason": reason}
    except PyMongoError as e:
        logger.error(
            "Error updating llm_debug_data for request_id=%s: %s",
            request_id,
            e,
            exc_info=True,
        )
        raise ChatHistoryServiceError(
            f"Could not update llm_debug_data for request_id {request_id}: {e}"
        ) from e


def _upsert_turn_execution_projection_for_message(
    *,
    message: Dict[str, Any],
    llm_debug_data: Optional[Dict[str, Any]],
    user_id: str,
    session_id: str,
    namespace: Optional[str],
    org_id: Optional[str],
) -> None:
    if message.get("role") != "assistant":
        return
    if not isinstance(llm_debug_data, dict):
        return

    record = llm_debug_data.get("turn_execution_record")
    if not isinstance(record, dict):
        return

    try:
        outcome = upsert_turn_execution_record_projection(
            record=record,
            user_id=user_id,
            session_id=session_id,
            namespace=namespace,
            org_id=org_id,
        )
        if isinstance(outcome, dict) and not outcome.get("updated", False):
            reason = outcome.get("reason")
            if isinstance(reason, str) and reason:
                logger.debug(
                    "turn_execution_records projection not updated for request_id=%s (%s)",
                    record.get("request_id"),
                    reason,
                )
        if _is_agent_test_instance():
            logger.debug(
                "Skipping episode_critique_memory backfill for request_id=%s in AgentTest",
                record.get("request_id"),
            )
            return
        critique_outcome = schedule_episode_critique_memory_from_turn(
            record=record,
            llm_debug_data=llm_debug_data,
            user_id=user_id,
            session_id=session_id,
            namespace=namespace,
            org_id=org_id,
        )
        if isinstance(critique_outcome, dict) and not critique_outcome.get(
            "scheduled", False
        ):
            reason = critique_outcome.get("reason")
            if isinstance(reason, str) and reason not in {"already_scheduled"}:
                logger.debug(
                    "episode_critique_memory not scheduled for request_id=%s (%s)",
                    record.get("request_id"),
                    reason,
                )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Failed to persist turn_execution projections for request_id=%s: %s",
            record.get("request_id"),
            exc,
        )


def add_message_to_history(
    user_id: str,
    session_id: str,
    message: Dict[str, Any],
    llm_debug_data: Optional[Dict[str, Any]] = None,
    *,
    broadcast_to_shared: Optional[bool] = None,
    exclude_user_from_broadcast: Optional[str] = None,
    namespace: Optional[str] = None,
    organisation_concept_id: Optional[str] = None,
    role_in_org: Optional[str] = None,
    skip_rag_indexing: bool = False,
) -> None:
    """
    Adds a message to the chat history for a specific user and session.

    Args:
        user_id: The user's concept ID
        session_id: The session UUID
        message: Message dictionary with 'role' and 'content' keys
        llm_debug_data: Optional LLM debug information (model, context stats, tool usage, etc.)
        broadcast_to_shared: If True, broadcast the turn to SSE subscribers.
            If None (default), auto-detect based on whether the session has subscribers.
        exclude_user_from_broadcast: User ID to exclude from broadcast (typically the author)
        skip_rag_indexing: When True, persist the turn without enqueuing best-effort
            chat-history RAG indexing. Useful for deterministic fixture seeding and
            other fast-path writes that should not wait on embeddings.
    """
    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")

    if not message or "role" not in message or "content" not in message:
        raise ChatHistoryServiceError("Message must contain 'role' and 'content' keys.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        # Get session context for organisation/role/namespace (best effort), then
        # apply any explicit call-site overrides so write and index paths stay aligned
        # with the request's effective namespace resolution.
        session_context = get_session_context()
        effective_session_context = dict(session_context)

        if isinstance(namespace, str) and namespace.strip():
            effective_session_context["namespace"] = namespace.strip()
        if isinstance(organisation_concept_id, str) and organisation_concept_id.strip():
            org_value = organisation_concept_id.strip()
            effective_session_context["organisation_concept_id"] = org_value
            effective_session_context["org_id"] = org_value
        if isinstance(role_in_org, str) and role_in_org.strip():
            effective_session_context["role_in_org"] = role_in_org.strip()

        # Determine namespace used for both persistence and RAG indexing.
        ns = _derive_rag_namespace(
            session_context=effective_session_context, user_id=user_id
        )

        org_concept_id = effective_session_context.get("organisation_concept_id")
        org_id_value = (
            org_concept_id.strip()
            if isinstance(org_concept_id, str) and org_concept_id.strip()
            else None
        )

        # Add timestamp to message (and llm_debug_data if present)
        message_with_timestamp = {**message, "timestamp": datetime.now(timezone.utc)}
        author_user_id = message.get("author_user_id")
        if not isinstance(author_user_id, str) or not author_user_id.strip():
            author_user_id = None
        if not author_user_id and message.get("role") == "user":
            author_user_id = user_id
        if author_user_id:
            message_with_timestamp["author_user_id"] = author_user_id
        llm_debug_payload = _ensure_turn_execution_record_for_assistant_message(
            message=message,
            llm_debug_data=llm_debug_data,
            user_id=user_id,
            session_id=session_id,
            namespace=ns.strip() if isinstance(ns, str) and ns.strip() else None,
            org_id=org_id_value,
            interaction_timestamp_utc=message_with_timestamp["timestamp"].isoformat(),
        )
        if llm_debug_data:
            request_id_value = (
                str(llm_debug_payload.get("request_id")).strip()
                if isinstance(llm_debug_payload, dict)
                and llm_debug_payload.get("request_id")
                else None
            )
            compacted_debug = compact_debug_payload_for_storage(
                llm_debug_payload,
                root_kind="chat_history.llm_debug_data",
                namespace=ns.strip() if isinstance(ns, str) and ns.strip() else None,
                request_id=request_id_value,
                fail_soft=True,
            )
            message_with_timestamp["llm_debug_data"] = compacted_debug.payload

        compacted_message = compact_debug_payload_for_storage(
            message_with_timestamp,
            root_kind="chat_history.message",
            namespace=ns.strip() if isinstance(ns, str) and ns.strip() else None,
            request_id=(
                str(llm_debug_payload.get("request_id")).strip()
                if isinstance(llm_debug_payload, dict)
                and llm_debug_payload.get("request_id")
                else None
            ),
            fail_soft=True,
        )
        stored_message = (
            compacted_message.payload
            if isinstance(compacted_message.payload, dict)
            else message_with_timestamp
        )

        set_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
        # NOTE: namespace is intentionally NOT in $set - it should only be set
        # on document creation via $setOnInsert. Otherwise, when a shared
        # conversation participant adds a message, their namespace would
        # overwrite the owner's namespace and cause the conversation to
        # disappear from the owner's session list. (JVNAUTOSCI-1004)

        session_name = None
        if message.get("role") == "user":
            session_name = _session_name_from_message(message.get("content"))

        set_on_insert: Dict[str, Any] = {"created_at": datetime.now(timezone.utc)}
        if isinstance(ns, str) and ns.strip():
            set_on_insert["namespace"] = ns.strip()
        if session_name:
            set_on_insert["session_name"] = session_name
        if isinstance(org_concept_id, str) and org_concept_id.strip():
            set_on_insert["organisation_concept_id"] = org_concept_id.strip()
        role_value = effective_session_context.get("role_in_org")
        if isinstance(role_value, str) and role_value.strip():
            set_on_insert["role_in_org"] = role_value.strip()

        # Update or insert the session document
        chat_history_coll.update_one(
            {"user_id": user_id, "session_id": session_id},
            {
                "$push": {"history": stored_message},
                "$set": set_fields,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )

        logger.debug(
            f"Added message to history for user {user_id}, session {session_id}"
        )

        _upsert_turn_execution_projection_for_message(
            message=message,
            llm_debug_data=llm_debug_payload,
            user_id=user_id,
            session_id=session_id,
            namespace=ns.strip() if isinstance(ns, str) and ns.strip() else None,
            org_id=org_id_value,
        )

        # Index to RAG (Best effort)
        if get_rag_service and not skip_rag_indexing:
            try:
                rag = get_rag_service()
                content = message.get("content", "")
                # Only index string content that isn't empty
                if isinstance(content, str) and content.strip():
                    # Skip indexing tool outputs that are just "truncated" markers or very small
                    content = _truncate_for_rag(content)

                    # Get session context for organisation and role
                    role = message.get("role", "unknown")
                    doc = {
                        "id": str(uuid.uuid4()),
                        "text": content,
                        "metadata": _build_rag_metadata(
                            session_context=effective_session_context,
                            user_id=user_id,
                            session_id=session_id,
                            role=role,
                            channel=None,
                        ),
                    }
                    if not isinstance(ns, str) or not ns.strip():
                        ns = "chat_history"
                    rag.upsert_documents([doc], namespace=ns)

                    # If this is an assistant message in presenter mode, also index the spoken talk track.
                    if role == "assistant" and isinstance(llm_debug_data, dict):
                        presenter_channels = llm_debug_data.get("presenter_channels")
                        if isinstance(presenter_channels, dict):
                            spoken = presenter_channels.get("spoken")
                            if isinstance(spoken, str) and spoken.strip():
                                spoken_txt = spoken.strip()
                                if spoken_txt != content.strip():
                                    spoken_doc = {
                                        "id": str(uuid.uuid4()),
                                        "text": _truncate_for_rag(spoken_txt),
                                        "metadata": _build_rag_metadata(
                                            session_context=effective_session_context,
                                            user_id=user_id,
                                            session_id=session_id,
                                            role=role,
                                            channel="spoken",
                                        ),
                                    }
                                    rag.upsert_documents([spoken_doc], namespace=ns)

                    # Track indexing progress per chat session for status UI.
                    try:
                        chat_history_coll.update_one(
                            {"user_id": user_id, "session_id": session_id},
                            {"$inc": {"rag_indexed_success": 1}},
                        )
                    except Exception:
                        pass
            except Exception as e:
                # Log but don't fail the chat request
                logger.warning(f"Failed to index chat message to RAG: {e}")
                try:
                    chat_history_coll.update_one(
                        {"user_id": user_id, "session_id": session_id},
                        {"$inc": {"rag_indexed_failed": 1}},
                    )
                except Exception:
                    pass

        # Broadcast to shared conversation subscribers (JVNAUTOSCI-1002)
        # Auto-detect if session has subscribers when broadcast_to_shared is None
        should_broadcast = broadcast_to_shared
        if should_broadcast is None:
            try:
                from .shared_conversation_stream_service import get_stream_service

                should_broadcast = (
                    get_stream_service().get_subscriber_count(session_id) > 0
                )
            except Exception:
                should_broadcast = False

        if should_broadcast:
            try:
                from .shared_conversation_stream_service import broadcast_shared_turn

                role = message.get("role", "unknown")
                content = message.get("content", "")
                history_index = None
                try:
                    query = build_chat_history_query(
                        user_id=user_id,
                        session_id=session_id,
                        namespace=ns,
                        include_legacy=True,
                    )
                    history_length = None
                    if hasattr(chat_history_coll, "aggregate") and callable(
                        getattr(chat_history_coll, "aggregate")
                    ):
                        try:
                            doc = next(
                                chat_history_coll.aggregate(
                                    [
                                        {"$match": query},
                                        {
                                            "$project": {
                                                "history_length": {"$size": "$history"}
                                            }
                                        },
                                    ]
                                ),
                                None,
                            )
                        except Exception:
                            doc = None
                        if doc and isinstance(doc.get("history_length"), int):
                            history_length = doc["history_length"]
                    if history_length is None:
                        doc = chat_history_coll.find_one(query, {"history": 1})
                        if doc is not None:
                            history = doc.get("history") or []
                            if isinstance(history, list):
                                history_length = len(history)
                    if isinstance(history_length, int) and history_length > 0:
                        history_index = history_length - 1
                except Exception:
                    history_index = None
                # Generate a turn_id from session+timestamp for deduplication
                turn_id = (
                    f"{session_id}_{message_with_timestamp['timestamp'].isoformat()}"
                )
                notified = broadcast_shared_turn(
                    session_id=session_id,
                    turn_id=turn_id,
                    speaker=role,
                    content=content,
                    created_at=message_with_timestamp.get("timestamp"),
                    author_user_id=author_user_id,
                    history_index=history_index,
                    exclude_user_id=exclude_user_from_broadcast,
                )

                # Log episode for cross-user delivery (JVNAUTOSCI-1002)
                if notified > 0:
                    try:
                        from .episode_logging_service import log_episode

                        log_episode(
                            episode_type="shared_conversation_turn_broadcast",
                            actor_user_id=author_user_id or user_id,
                            session_id=session_id,
                            payload={
                                "turn_id": turn_id,
                                "speaker": role,
                                "recipients_notified": notified,
                                "content_length": len(content) if content else 0,
                            },
                            status="delivered",
                        )
                    except Exception:
                        pass  # Episode logging is best-effort
            except Exception as e:
                # Best effort - don't fail the main operation
                logger.warning(f"Failed to broadcast shared turn: {e}")

    except PyMongoError as e:
        logger.error(f"Error adding message to history: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not add message to history: {e}") from e


def set_chat_history_for_session(
    *,
    user_id: str,
    session_id: str,
    history: List[Dict[str, Any]],
    namespace: Optional[str] = None,
    set_updated_at: bool = False,
    extra_set_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")
    if not isinstance(history, list):
        raise ChatHistoryServiceError("history must be a list.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    set_fields: Dict[str, Any] = {"history": history}
    if isinstance(namespace, str) and namespace.strip():
        set_fields["namespace"] = namespace.strip()
    if set_updated_at:
        set_fields["updated_at"] = datetime.now(timezone.utc)
    if isinstance(extra_set_fields, dict) and extra_set_fields:
        set_fields.update(extra_set_fields)

    query = build_chat_history_query(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_legacy=True,
    )

    try:
        result = chat_history_coll.update_one(query, {"$set": set_fields})
        updated = bool(getattr(result, "modified_count", 0) > 0)
        matched = bool(getattr(result, "matched_count", 0) > 0)
        return {"updated": updated, "matched": matched}
    except PyMongoError as e:
        logger.error("Error setting chat history: %s", e, exc_info=True)
        raise ChatHistoryServiceError(f"Could not set chat history: {e}") from e


def add_reset_marker_to_history(user_id: str, session_id: str) -> None:
    """
    Adds a reset marker to the chat history instead of deleting it.
    This preserves the full conversation history while allowing the UI to display only post-reset messages.

    Args:
        user_id: The user's concept ID
        session_id: The session UUID
    """
    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        reset_marker = {
            "role": "system",
            "content": "__RESET__",
            "timestamp": datetime.now(timezone.utc),
        }

        result = chat_history_coll.update_one(
            {"user_id": user_id, "session_id": session_id},
            {
                "$push": {"history": reset_marker},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )

        if result.matched_count > 0:
            logger.info(f"Added reset marker for user {user_id}, session {session_id}")
        else:
            logger.warning(
                f"No session found to add reset marker for user {user_id}, session {session_id}"
            )
    except PyMongoError as e:
        logger.error(f"Error adding reset marker: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not add reset marker: {e}") from e


def delete_chat_history(user_id: str, session_id: str) -> None:
    """
    Deletes the chat history for a specific user and session.
    Note: This is kept for backward compatibility but add_reset_marker_to_history() is preferred.

    Args:
        user_id: The user's concept ID
        session_id: The session UUID
    """
    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        result = chat_history_coll.delete_one(
            {"user_id": user_id, "session_id": session_id}
        )

        if result.deleted_count > 0:
            logger.info(
                f"Deleted chat history for user {user_id}, session {session_id}"
            )
        else:
            logger.warning(
                f"No history found to delete for user {user_id}, session {session_id}"
            )
    except PyMongoError as e:
        logger.error(f"Error deleting chat history: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not delete chat history: {e}") from e


def get_chat_history_length(
    user_id: str,
    *,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
) -> int:
    """
    Retrieves the total number of chat turns across ALL sessions for a user.
    Used to display "History: NNN" in the footer.

    Args:
        user_id: The user's concept ID

    Returns:
        Total count of messages across all sessions
    """
    if not user_id:
        raise ChatHistoryServiceError("user_id is required.")

    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    _guard_chat_history_read("get_chat_history_length")

    try:
        query = build_chat_history_query(
            user_id=user_id, namespace=namespace, include_legacy=include_legacy
        )
        pipeline = [
            {"$match": query},
            {
                "$project": {
                    "_id": 0,
                    "message_count": {
                        "$size": {
                            "$filter": {
                                "input": _history_array_expr(),
                                "as": "msg",
                                "cond": {
                                    "$not": {
                                        "$and": [
                                            {"$eq": ["$$msg.role", "system"]},
                                            {"$eq": ["$$msg.content", "__RESET__"]},
                                        ]
                                    }
                                },
                            }
                        }
                    },
                }
            },
            {"$group": {"_id": None, "total_turns": {"$sum": "$message_count"}}},
        ]
        summary = next(
            _read_aggregate(
                chat_history_coll,
                pipeline,
                operation="get_chat_history_length.aggregate",
            ),
            None,
        )
        total_turns = 0
        if isinstance(summary, dict):
            raw_total = summary.get("total_turns")
            if isinstance(raw_total, int) and raw_total >= 0:
                total_turns = raw_total
        _record_chat_history_read_success()
        return total_turns
    except PyMongoError as e:
        _record_chat_history_read_failure("get_chat_history_length", e)
        logger.error(f"Error retrieving chat history length: {e}", exc_info=True)
        raise ChatHistoryServiceError(
            f"Could not retrieve chat history length: {e}"
        ) from e


def get_chat_history_session_count(
    user_id: str,
    *,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
) -> int:
    """Return number of sessions for a user that contain messages or an explicit name."""
    if not user_id:
        raise ChatHistoryServiceError("user_id is required.")

    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    _guard_chat_history_read("get_chat_history_session_count")

    try:
        query = build_chat_history_query(
            user_id=user_id, namespace=namespace, include_legacy=include_legacy
        )
        pipeline = [
            {"$match": query},
            {
                "$project": {
                    "_id": 0,
                    "message_count": {
                        "$size": {
                            "$filter": {
                                "input": _history_array_expr(),
                                "as": "msg",
                                "cond": {
                                    "$not": {
                                        "$and": [
                                            {"$eq": ["$$msg.role", "system"]},
                                            {"$eq": ["$$msg.content", "__RESET__"]},
                                        ]
                                    }
                                },
                            }
                        }
                    },
                    "has_name": {
                        "$gt": [
                            {
                                "$strLenCP": {
                                    "$trim": {
                                        "input": {"$ifNull": ["$session_name", ""]}
                                    }
                                }
                            },
                            0,
                        ]
                    },
                }
            },
            {
                "$group": {
                    "_id": None,
                    "session_count": {
                        "$sum": {
                            "$cond": [
                                {"$or": [{"$gt": ["$message_count", 0]}, "$has_name"]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
        ]
        summary = next(
            _read_aggregate(
                chat_history_coll,
                pipeline,
                operation="get_chat_history_session_count.aggregate",
            ),
            None,
        )
        count = 0
        if isinstance(summary, dict):
            raw_count = summary.get("session_count")
            if isinstance(raw_count, int) and raw_count >= 0:
                count = raw_count
        _record_chat_history_read_success()
        return count
    except PyMongoError as e:
        _record_chat_history_read_failure("get_chat_history_session_count", e)
        logger.error(f"Error retrieving chat history session count: {e}", exc_info=True)
        raise ChatHistoryServiceError(
            f"Could not retrieve chat history session count: {e}"
        ) from e


def _build_session_summary_from_metadata(
    doc: Dict[str, Any],
    *,
    session_id_override: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    session_id = (
        session_id_override
        if isinstance(session_id_override, str) and session_id_override
        else doc.get("session_id")
    )
    if not isinstance(session_id, str) or not session_id:
        return None

    session_name = _normalise_session_name(doc.get("session_name"))
    created_ts = _infer_created_timestamp(doc)
    last_ts = _coerce_datetime(doc.get("updated_at")) or created_ts

    # Metadata-only summaries intentionally avoid history reads for resilience.
    if not session_name and last_ts is None and created_ts is None:
        return None

    provenance = _session_provenance_from_doc(doc)
    return {
        "session_id": session_id,
        "session_name": session_name,
        "message_count": None,
        "last_message_at": last_ts.isoformat() if last_ts else None,
        "is_completed": False,
        "completed_at": None,
        "created_at": created_ts.isoformat() if created_ts else None,
        "namespace": doc.get("namespace"),
        "organisation_concept_id": doc.get("organisation_concept_id"),
        "preview": None,
        **provenance,
    }


def _get_chat_history_session_summaries_metadata_only(
    chat_history_coll,
    *,
    query: Dict[str, Any],
    safe_limit: int | None,
) -> List[Dict[str, Any]]:
    projection: Dict[str, Any] = _add_chat_session_provenance_projection(
        {
            "_id": 0,
            "session_id": 1,
            "session_name": 1,
            "created_at": 1,
            "updated_at": 1,
            "namespace": 1,
            "organisation_concept_id": 1,
        }
    )
    cursor = _read_find(
        chat_history_coll,
        query,
        projection,
        operation="get_chat_history_session_summaries.metadata_find",
    )
    try:
        cursor = cursor.sort(
            [("updated_at", DESCENDING), ("created_at", DESCENDING)]
        )
    except Exception:
        pass
    if safe_limit is not None:
        try:
            cursor = cursor.limit(safe_limit)
        except AttributeError:
            pass
        try:
            cursor = cursor.batch_size(safe_limit)
        except Exception:
            pass
    docs = list(cursor)

    summaries: List[Dict[str, Any]] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        summary = _build_session_summary_from_metadata(doc)
        if summary is None:
            continue
        summaries.append(summary)

    summaries.sort(
        key=lambda s: _coerce_datetime(s.get("last_message_at"))
        or datetime(1970, 1, 1, tzinfo=timezone.utc),
        reverse=True,
    )
    if safe_limit is None:
        return summaries
    return summaries[:safe_limit]


def _bounded_light_session_metadata_query_limit(
    *,
    safe_limit: int,
    agent_visibility: Any,
    keep_newest_agent_created: Any,
) -> int:
    visibility = _normalise_chat_session_agent_visibility(agent_visibility)
    keep_newest = _coerce_bool(keep_newest_agent_created, default=True)
    if visibility == CHAT_SESSION_AGENT_VISIBILITY_EXCLUDE or (
        visibility == CHAT_SESSION_AGENT_VISIBILITY_INCLUDE and keep_newest
    ):
        return max(safe_limit, min(300, safe_limit + 50))
    return safe_limit


def _safe_chat_history_session_summary_limit(limit: Any) -> int:
    safe_limit = 50
    if isinstance(limit, int) and limit > 0:
        safe_limit = min(limit, 500)
    return safe_limit


def _load_chat_history_session_summaries(
    user_id: str,
    *,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    summary_mode: str = "full",
    metadata_query_limit: int | None = None,
) -> List[Dict[str, Any]]:
    if not user_id:
        raise ChatHistoryServiceError("user_id is required.")

    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    _guard_chat_history_read("get_chat_history_session_summaries")

    mode = summary_mode.strip().lower() if isinstance(summary_mode, str) else "full"
    light_mode = mode in ("light", "minimal", "summary")

    query = build_chat_history_query(
        user_id=user_id, namespace=namespace, include_legacy=include_legacy
    )

    if light_mode:
        try:
            summaries = _get_chat_history_session_summaries_metadata_only(
                chat_history_coll, query=query, safe_limit=metadata_query_limit
            )
            _record_chat_history_read_success()
            return summaries
        except PyMongoError as e:
            _record_chat_history_read_failure("get_chat_history_session_summaries", e)
            logger.error(
                "Error retrieving metadata-only chat history session summaries: %s",
                e,
                exc_info=True,
            )
            raise ChatHistoryServiceError(
                f"Could not retrieve chat history session summaries: {e}"
            ) from e

    try:
        projection: Dict[str, Any] = _add_chat_session_provenance_projection(
            {
                "session_id": 1,
                "history": 1,
                "created_at": 1,
                "updated_at": 1,
                "namespace": 1,
                "organisation_concept_id": 1,
                "session_name": 1,
            }
        )
        docs = list(
            _read_find(
                chat_history_coll,
                query,
                projection,
                operation="get_chat_history_session_summaries.full_find",
            )
        )
    except PyMongoError as e:
        _record_chat_history_read_failure("get_chat_history_session_summaries", e)
        # Atlas timeout resilience: fall back to metadata-only summaries when
        # fetching full history payloads is too expensive.
        logger.warning(
            "Full chat history session summaries failed; falling back to metadata-only summaries: %s",
            e,
            exc_info=True,
        )
        try:
            summaries = _get_chat_history_session_summaries_metadata_only(
                chat_history_coll, query=query, safe_limit=None
            )
            _record_chat_history_read_success()
            return summaries
        except PyMongoError as fallback_error:
            _record_chat_history_read_failure(
                "get_chat_history_session_summaries", fallback_error
            )
            logger.error(
                "Metadata-only fallback for chat history session summaries also failed: %s",
                fallback_error,
                exc_info=True,
            )
            raise ChatHistoryServiceError(
                f"Could not retrieve chat history session summaries: {e}"
            ) from fallback_error

    try:
        summaries: List[Dict[str, Any]] = []
        for doc in docs:
            session_id = doc.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue

            history = doc.get("history") or []
            if not isinstance(history, list):
                history = []

            session_name = _normalise_session_name(doc.get("session_name"))
            non_reset = list(_iter_non_reset_messages(history))
            has_messages = bool(non_reset)
            if not has_messages and not session_name:
                continue

            last_entry = None
            for entry in reversed(history):
                if isinstance(entry, dict):
                    last_entry = entry
                    break

            is_completed = bool(last_entry and _is_reset_marker(last_entry))
            completed_at_dt = None
            if is_completed and isinstance(last_entry, dict):
                completed_at_dt = _coerce_datetime(last_entry.get("timestamp"))

            last_ts = _infer_last_message_timestamp(doc)
            created_ts = _infer_created_timestamp(doc)

            message_count = len(non_reset)
            last_user_msg = None
            for msg in reversed(non_reset):
                if msg.get("role") == "user":
                    content = msg.get("content")
                    if isinstance(content, str) and content.strip():
                        last_user_msg = content.strip()
                        break

            preview = last_user_msg
            if isinstance(preview, str) and len(preview) > 140:
                preview = preview[:140] + "."

            provenance = _session_provenance_from_doc(doc)
            summaries.append(
                {
                    "session_id": session_id,
                    "session_name": session_name,
                    "message_count": message_count,
                    "last_message_at": last_ts.isoformat() if last_ts else None,
                    "is_completed": is_completed,
                    "completed_at": (
                        completed_at_dt.isoformat() if completed_at_dt else None
                    ),
                    "created_at": created_ts.isoformat() if created_ts else None,
                    "namespace": doc.get("namespace"),
                    "organisation_concept_id": doc.get("organisation_concept_id"),
                    "preview": preview,
                    **provenance,
                }
            )

        summaries.sort(
            key=lambda s: _coerce_datetime(s.get("last_message_at"))
            or datetime(1970, 1, 1, tzinfo=timezone.utc),
            reverse=True,
        )
        _record_chat_history_read_success()
        return summaries
    except PyMongoError as e:
        _record_chat_history_read_failure("get_chat_history_session_summaries", e)
        logger.error(
            f"Error retrieving chat history session summaries: {e}", exc_info=True
        )
        raise ChatHistoryServiceError(
            f"Could not retrieve chat history session summaries: {e}"
        ) from e


def get_chat_history_session_summaries_result(
    user_id: str,
    limit: int = 50,
    *,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    summary_mode: str = "full",
    agent_visibility: Any = CHAT_SESSION_AGENT_VISIBILITY_INCLUDE,
    keep_newest_agent_created: Any = True,
) -> Dict[str, Any]:
    """Return session summaries plus test-conversation visibility metadata."""

    safe_limit = _safe_chat_history_session_summary_limit(limit)
    metadata_query_limit = None
    mode = summary_mode.strip().lower() if isinstance(summary_mode, str) else "full"
    if mode in ("light", "minimal", "summary"):
        metadata_query_limit = _bounded_light_session_metadata_query_limit(
            safe_limit=safe_limit,
            agent_visibility=agent_visibility,
            keep_newest_agent_created=keep_newest_agent_created,
        )
    summaries = _load_chat_history_session_summaries(
        user_id,
        namespace=namespace,
        include_legacy=include_legacy,
        summary_mode=summary_mode,
        metadata_query_limit=metadata_query_limit,
    )
    visibility_payload = apply_chat_session_agent_visibility(
        summaries,
        agent_visibility=agent_visibility,
        keep_newest_agent_created=keep_newest_agent_created,
        limit=safe_limit,
    )
    return {
        "sessions": visibility_payload["sessions"],
        "limit": safe_limit,
        "metadata_query_limit": metadata_query_limit,
        "raw_session_count_is_bounded": metadata_query_limit is not None,
        "raw_session_count": len(summaries),
        **{
            key: value
            for key, value in visibility_payload.items()
            if key not in {"sessions", "limit"}
        },
    }


def get_chat_history_session_summaries(
    user_id: str,
    limit: int = 50,
    *,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    summary_mode: str = "full",
) -> List[Dict[str, Any]]:
    """Return per-session summaries ordered by inferred last message timestamp desc.

    summary_mode="light" is metadata-only to avoid large history reads.
    """

    result = get_chat_history_session_summaries_result(
        user_id,
        limit=limit,
        namespace=namespace,
        include_legacy=include_legacy,
        summary_mode=summary_mode,
        agent_visibility=CHAT_SESSION_AGENT_VISIBILITY_INCLUDE,
    )
    return result["sessions"]


def has_chat_history_session(
    user_id: str,
    session_id: str,
    *,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
) -> bool:
    if not user_id or not session_id:
        return False

    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    _guard_chat_history_read("has_chat_history_session")

    query = build_chat_history_query(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_legacy=include_legacy,
    )
    try:
        result = (
            _read_find_one(
                chat_history_coll,
                query,
                {"_id": 1},
                operation="has_chat_history_session.exists",
            )
            is not None
        )
        _record_chat_history_read_success()
        return result
    except PyMongoError as e:
        _record_chat_history_read_failure("has_chat_history_session", e)
        raise ChatHistoryServiceError(
            f"Could not check chat history session: {e}"
        ) from e


def get_chat_history_session_summary(
    user_id: str,
    session_id: str,
    *,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    summary_mode: str = "full",
) -> Optional[Dict[str, Any]]:
    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")

    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    _guard_chat_history_read("get_chat_history_session_summary")

    mode = summary_mode.strip().lower() if isinstance(summary_mode, str) else "full"
    light_mode = mode in ("light", "minimal", "summary")

    metadata_projection: Dict[str, Any] = _add_chat_session_provenance_projection(
        {
            "_id": 0,
            "session_id": 1,
            "session_name": 1,
            "created_at": 1,
            "updated_at": 1,
            "namespace": 1,
            "organisation_concept_id": 1,
        }
    )
    if light_mode:
        try:
            metadata_doc = None
            for session_query in _build_chat_history_session_read_queries(
                user_id=user_id,
                session_id=session_id,
                namespace=namespace,
                include_legacy=include_legacy,
            ):
                metadata_doc = _read_find_one(
                    chat_history_coll,
                    session_query,
                    metadata_projection,
                    operation="get_chat_history_session_summary.metadata_find",
                )
                if metadata_doc is not None:
                    break
        except PyMongoError as e:
            _record_chat_history_read_failure("get_chat_history_session_summary", e)
            logger.error(
                "Error retrieving metadata-only chat history session summary: %s",
                e,
                exc_info=True,
            )
            raise ChatHistoryServiceError(
                f"Could not retrieve chat history session summary: {e}"
            ) from e
        if not isinstance(metadata_doc, dict):
            _record_chat_history_read_success()
            return None
        summary = _build_session_summary_from_metadata(
            metadata_doc, session_id_override=session_id
        )
        _record_chat_history_read_success()
        return summary

    projection: Dict[str, Any] = _add_chat_session_provenance_projection(
        {
            "session_id": 1,
            "history": 1,
            "created_at": 1,
            "updated_at": 1,
            "namespace": 1,
            "organisation_concept_id": 1,
            "session_name": 1,
        }
    )
    try:
        doc = None
        for session_query in _build_chat_history_session_read_queries(
            user_id=user_id,
            session_id=session_id,
            namespace=namespace,
            include_legacy=include_legacy,
        ):
            doc = _read_find_one(
                chat_history_coll,
                session_query,
                projection,
                operation="get_chat_history_session_summary.full_find",
            )
            if doc is not None:
                break
    except PyMongoError as e:
        _record_chat_history_read_failure("get_chat_history_session_summary", e)
        logger.warning(
            "Full chat history session summary failed; falling back to metadata-only summary: %s",
            e,
            exc_info=True,
        )
        try:
            metadata_doc = None
            for session_query in _build_chat_history_session_read_queries(
                user_id=user_id,
                session_id=session_id,
                namespace=namespace,
                include_legacy=include_legacy,
            ):
                metadata_doc = _read_find_one(
                    chat_history_coll,
                    session_query,
                    metadata_projection,
                    operation="get_chat_history_session_summary.metadata_fallback",
                )
                if metadata_doc is not None:
                    break
        except PyMongoError as fallback_error:
            _record_chat_history_read_failure(
                "get_chat_history_session_summary", fallback_error
            )
            logger.error(
                "Metadata-only fallback for chat history session summary failed: %s",
                fallback_error,
                exc_info=True,
            )
            raise ChatHistoryServiceError(
                f"Could not retrieve chat history session summary: {e}"
            ) from fallback_error
        if not isinstance(metadata_doc, dict):
            _record_chat_history_read_success()
            return None
        summary = _build_session_summary_from_metadata(
            metadata_doc, session_id_override=session_id
        )
        _record_chat_history_read_success()
        return summary

    if not isinstance(doc, dict):
        _record_chat_history_read_success()
        return None

    history = doc.get("history") or []
    if not isinstance(history, list):
        history = []

    session_name = _normalise_session_name(doc.get("session_name"))
    non_reset = list(_iter_non_reset_messages(history))
    has_messages = bool(non_reset)
    if not has_messages and not session_name:
        return None

    last_entry = None
    for entry in reversed(history):
        if isinstance(entry, dict):
            last_entry = entry
            break

    is_completed = bool(last_entry and _is_reset_marker(last_entry))
    completed_at_dt = None
    if is_completed and isinstance(last_entry, dict):
        completed_at_dt = _coerce_datetime(last_entry.get("timestamp"))

    last_ts = _infer_last_message_timestamp(doc)
    created_ts = _infer_created_timestamp(doc)

    message_count = len(non_reset)
    last_user_msg = None
    for msg in reversed(non_reset):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                last_user_msg = content.strip()
                break

    preview = last_user_msg
    if isinstance(preview, str) and len(preview) > 140:
        preview = preview[:140] + "."

    provenance = _session_provenance_from_doc(doc)
    summary = {
        "session_id": session_id,
        "session_name": session_name,
        "message_count": message_count,
        "last_message_at": last_ts.isoformat() if last_ts else None,
        "is_completed": is_completed,
        "completed_at": completed_at_dt.isoformat() if completed_at_dt else None,
        "created_at": created_ts.isoformat() if created_ts else None,
        "namespace": doc.get("namespace"),
        "organisation_concept_id": doc.get("organisation_concept_id"),
        "preview": preview,
        **provenance,
    }
    _record_chat_history_read_success()
    return summary


def create_chat_session(
    *,
    user_id: str,
    session_id: str,
    session_name: Optional[str] = None,
    namespace: Optional[str] = None,
    organisation_concept_id: Optional[str] = None,
    role_in_org: Optional[str] = None,
    origin_kind: Optional[str] = None,
    created_by_actor_concept_id: Optional[str] = None,
    created_by_actor_type: Optional[str] = None,
    is_agent_created: Optional[bool] = None,
    test_artifact_kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new chat session document if it does not already exist.

    Args:
        user_id: The user concept ID
        session_id: The session UUID
        session_name: Optional display name for the session
        namespace: Optional explicit namespace (e.g., from window session context)
        organisation_concept_id: Optional explicit org ID (e.g., from window session context)
        role_in_org: Optional explicit role (e.g., from window session context)
        origin_kind: Optional durable provenance kind for non-human/test sessions
        created_by_actor_concept_id: Optional Vontology actor concept attribution
        created_by_actor_type: Optional actor type concept, such as #V#coding_agent
        is_agent_created: Optional explicit test/agent-created flag
        test_artifact_kind: Optional test/benchmark fixture class

    If namespace/org/role not provided, falls back to Flask session context.
    """
    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id:
        raise ChatHistoryServiceError("session_id is required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    now = datetime.now(timezone.utc)

    # JVNAUTOSCI-1011: Prefer explicit context params, fall back to Flask session
    session_context = get_session_context()
    effective_namespace = namespace or session_context.get("namespace")
    effective_org = organisation_concept_id or session_context.get(
        "organisation_concept_id"
    )
    effective_role = role_in_org or session_context.get("role_in_org")

    # Derive namespace from context if not explicitly provided
    ns = effective_namespace
    if not ns:
        ns = _derive_rag_namespace(
            session_context={"organisation_concept_id": effective_org}, user_id=user_id
        )
    name = _normalise_session_name(session_name) or _default_session_name(now)

    set_on_insert: Dict[str, Any] = {
        "created_at": now,
        "updated_at": now,
        "session_name": name,
        "history": [],
    }
    if isinstance(ns, str) and ns.strip():
        set_on_insert["namespace"] = ns.strip()
    if isinstance(effective_org, str) and effective_org.strip():
        set_on_insert["organisation_concept_id"] = effective_org.strip()
    if isinstance(effective_role, str) and effective_role.strip():
        set_on_insert["role_in_org"] = effective_role.strip()

    provenance_fields = _normalise_chat_session_provenance_fields(
        origin_kind=origin_kind,
        created_by_actor_concept_id=created_by_actor_concept_id,
        created_by_actor_type=created_by_actor_type,
        is_agent_created=is_agent_created,
        test_artifact_kind=test_artifact_kind,
    )
    update_payload: Dict[str, Any] = {"$setOnInsert": set_on_insert}
    if provenance_fields:
        # Provenance is intentionally safe to repair on an existing session
        # without changing updated_at or recency ordering.
        update_payload["$set"] = provenance_fields

    try:
        chat_history_coll.update_one(
            {"user_id": user_id, "session_id": session_id},
            update_payload,
            upsert=True,
        )
        projection = _add_chat_session_provenance_projection(
            {"session_name": 1, "namespace": 1}
        )
        doc = chat_history_coll.find_one(
            {"user_id": user_id, "session_id": session_id},
            projection,
        )
        stored_provenance = _session_provenance_from_doc(doc or {})
        return {
            "session_id": session_id,
            "session_name": _normalise_session_name(
                (doc or {}).get("session_name") or name
            ),
            "namespace": (doc or {}).get("namespace") or ns,
            **stored_provenance,
        }
    except PyMongoError as e:
        logger.error(f"Error creating chat session: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not create chat session: {e}") from e


def rename_chat_session(
    *,
    user_id: str,
    session_id: str,
    session_name: str,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
) -> Dict[str, Any]:
    """Rename an existing chat session without changing its recency ordering."""
    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id:
        raise ChatHistoryServiceError("session_id is required.")

    new_name = _normalise_session_name(session_name)
    if not new_name:
        raise ChatHistoryServiceError("session_name is required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        query = build_chat_history_query(
            user_id=user_id,
            session_id=session_id,
            namespace=namespace,
            include_legacy=include_legacy,
        )
        result = chat_history_coll.update_one(
            query,
            {"$set": {"session_name": new_name}},
        )
        updated = bool(getattr(result, "modified_count", 0) > 0)
        matched = bool(getattr(result, "matched_count", 0) > 0)
        return {"updated": updated, "matched": matched, "session_name": new_name}
    except PyMongoError as e:
        logger.error(f"Error renaming chat session: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not rename chat session: {e}") from e


def _normalise_concept_id_list(values: Any) -> List[str]:
    """Normalise a user-provided value into a de-duplicated list of concept IDs."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []

    seen: set[str] = set()
    result: List[str] = []
    for raw in values:
        if not isinstance(raw, str):
            continue
        item = raw.strip()
        if not item:
            continue
        if not item.startswith("#V#"):
            continue
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def get_chat_session_links(
    *,
    user_id: str,
    session_id: str,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
) -> Dict[str, List[str]]:
    """Fetch persisted concept links for a chat session.

    Returns a dict of lists keyed by: programmes, projects, activities, modalities.
    Missing/invalid stored values are normalised away.
    """
    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id:
        raise ChatHistoryServiceError("session_id is required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    query = build_chat_history_query(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_legacy=include_legacy,
    )

    try:
        doc = chat_history_coll.find_one(query, {"session_links": 1}) or {}
    except PyMongoError as e:
        logger.error(f"Error fetching chat session links: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not fetch session links: {e}") from e

    raw_links = doc.get("session_links")
    links = raw_links if isinstance(raw_links, dict) else {}
    return {
        "programmes": _normalise_concept_id_list(
            links.get("programmes")
            or links.get("programme_ids")
            or links.get("programme_concept_ids")
        ),
        "projects": _normalise_concept_id_list(
            links.get("projects")
            or links.get("project_ids")
            or links.get("project_concept_ids")
        ),
        "activities": _normalise_concept_id_list(
            links.get("activities")
            or links.get("activity_ids")
            or links.get("activity_concept_ids")
        ),
        "modalities": _normalise_concept_id_list(
            links.get("modalities")
            or links.get("modality_ids")
            or links.get("modality_concept_ids")
        ),
    }


def set_chat_session_links(
    *,
    user_id: str,
    session_id: str,
    session_links: Dict[str, Any],
    namespace: Optional[str] = None,
    include_legacy: bool = True,
) -> Dict[str, Any]:
    """Persist concept links for a chat session.

    Important: does NOT update session `updated_at` so recency ordering is stable.
    """
    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id:
        raise ChatHistoryServiceError("session_id is required.")

    if not isinstance(session_links, dict):
        session_links = {}

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    query = build_chat_history_query(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_legacy=include_legacy,
    )

    normalised = {
        "programmes": _normalise_concept_id_list(
            session_links.get("programmes")
            or session_links.get("programme_ids")
            or session_links.get("programme_concept_ids")
        ),
        "projects": _normalise_concept_id_list(
            session_links.get("projects")
            or session_links.get("project_ids")
            or session_links.get("project_concept_ids")
        ),
        "activities": _normalise_concept_id_list(
            session_links.get("activities")
            or session_links.get("activity_ids")
            or session_links.get("activity_concept_ids")
        ),
        "modalities": _normalise_concept_id_list(
            session_links.get("modalities")
            or session_links.get("modality_ids")
            or session_links.get("modality_concept_ids")
        ),
    }

    try:
        result = chat_history_coll.update_one(
            query,
            {
                "$set": {
                    "session_links": normalised,
                    "session_links_updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return {
            "updated": bool(getattr(result, "modified_count", 0) > 0),
            "matched": bool(getattr(result, "matched_count", 0) > 0),
            "session_links": normalised,
        }
    except PyMongoError as e:
        logger.error(f"Error setting chat session links: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not set session links: {e}") from e


def _agent_created_backfill_identity_query(doc: Dict[str, Any]) -> Dict[str, Any]:
    if doc.get("_id") is not None:
        return {"_id": doc["_id"]}
    return {
        "user_id": doc.get("user_id"),
        "session_id": doc.get("session_id"),
    }


def _agent_created_backfill_candidate(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    session_id = doc.get("session_id")
    session_name = _normalise_session_name(doc.get("session_name")) or ""
    if not isinstance(session_id, str) or not session_id.strip():
        return None

    sid = session_id.strip()
    if sid.startswith(_BROWSER_TEST_SESSION_ID_PREFIX):
        return {
            "reason": "browser_fixture_session_id",
            "fields": _normalise_chat_session_provenance_fields(
                origin_kind=CHAT_SESSION_ORIGIN_KIND_BROWSER_TEST_FIXTURE,
                created_by_actor_concept_id=VON_SYSTEM_ID,
                created_by_actor_type=CODING_AGENT_TYPE_ID,
                is_agent_created=True,
                test_artifact_kind="browser_test_fixture_chat_session",
            ),
        }

    if session_name.startswith(_BENCHMARK_SESSION_NAME_PREFIX):
        return {
            "reason": "benchmark_session_name",
            "fields": _normalise_chat_session_provenance_fields(
                origin_kind=CHAT_SESSION_ORIGIN_KIND_BENCHMARK_HARNESS,
                created_by_actor_concept_id=GITHUB_COPILOT_INSTANCE_ID,
                created_by_actor_type=CODING_AGENT_TYPE_ID,
                is_agent_created=True,
                test_artifact_kind="kb_clone_benchmark_chat_session",
            ),
        }

    if session_name.startswith(_LIVE_KB_TOOL_PROMPT_SAMPLER_SESSION_NAME_PREFIX):
        return {
            "reason": "live_kb_tool_prompt_sampler_session_name",
            "fields": _normalise_chat_session_provenance_fields(
                origin_kind=CHAT_SESSION_ORIGIN_KIND_CODING_AGENT_TEST,
                created_by_actor_concept_id=VON_SYSTEM_ID,
                created_by_actor_type=CODING_AGENT_TYPE_ID,
                is_agent_created=True,
                test_artifact_kind=_LIVE_KB_TOOL_PROMPT_SAMPLER_TEST_ARTIFACT_KIND,
            ),
        }

    return None


def backfill_agent_created_chat_session_provenance(
    *,
    user_concept_id: str,
    namespace: Optional[str] = None,
    include_legacy: bool = False,
    max_sessions: int = 500,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Mark reliable historical test/agent-created conversations.

    Only deterministic browser-test fixture session IDs, benchmark harness
    names, and live prompt sampler names are mutated. Ambiguous historical
    conversations remain untouched.
    """

    if not isinstance(user_concept_id, str) or not user_concept_id:
        raise ChatHistoryServiceError("user_concept_id is required")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    safe_max_sessions = 500
    if isinstance(max_sessions, int) and max_sessions > 0:
        safe_max_sessions = min(max_sessions, 10000)

    query = build_chat_history_query(
        user_id=user_concept_id,
        namespace=namespace,
        include_legacy=include_legacy,
    )
    projection = _add_chat_session_provenance_projection(
        {
            "_id": 1,
            "user_id": 1,
            "session_id": 1,
            "session_name": 1,
            "namespace": 1,
        }
    )

    sessions_examined = 0
    reliable_candidates: List[Dict[str, Any]] = []
    sessions_marked = 0
    already_marked = 0
    errors: List[Dict[str, Any]] = []

    try:
        cursor = chat_history_coll.find(query, projection)
        for doc in cursor:
            if sessions_examined >= safe_max_sessions:
                break
            if not isinstance(doc, dict):
                continue
            sessions_examined += 1

            session_id = doc.get("session_id")
            if not isinstance(session_id, str) or not session_id.strip():
                continue

            current_provenance = _session_provenance_from_doc(doc)
            if current_provenance.get("is_agent_created") is True:
                already_marked += 1
                continue

            candidate = _agent_created_backfill_candidate(doc)
            if not isinstance(candidate, dict):
                continue

            fields = candidate.get("fields")
            if not isinstance(fields, dict) or not fields:
                continue

            candidate_report = {
                "session_id": session_id,
                "session_name": _normalise_session_name(doc.get("session_name")),
                "reason": candidate.get("reason"),
                "proposed_fields": dict(fields),
            }
            reliable_candidates.append(candidate_report)

            if dry_run:
                continue

            set_fields = dict(fields)
            set_fields["agent_created_provenance_backfilled_at"] = datetime.now(
                timezone.utc
            )
            try:
                chat_history_coll.update_one(
                    _agent_created_backfill_identity_query(doc),
                    {"$set": set_fields},
                )
                sessions_marked += 1
            except Exception as exc:
                errors.append(
                    {
                        "type": "update_failed",
                        "session_id": session_id,
                        "error": str(exc),
                    }
                )

        return {
            "status": "ok",
            "user_concept_id": user_concept_id,
            "namespace": namespace,
            "include_legacy": include_legacy,
            "dry_run": bool(dry_run),
            "sessions_examined": sessions_examined,
            "reliable_candidate_count": len(reliable_candidates),
            "sessions_marked": sessions_marked,
            "already_marked": already_marked,
            "uncertain_candidate_count": 0,
            "uncertain_candidates": [],
            "reliable_candidates": reliable_candidates[:50],
            "errors": errors,
        }
    except PyMongoError as e:
        logger.error(
            "Error backfilling agent-created chat session provenance: %s",
            e,
            exc_info=True,
        )
        raise ChatHistoryServiceError(
            f"Could not backfill agent-created chat session provenance: {e}"
        ) from e


def backfill_chat_history_for_user(
    *,
    user_concept_id: str,
    target_namespace: str,
    organisation_concept_id: Optional[str] = None,
    role_in_org: Optional[str] = None,
    max_sessions: int = 10,
    max_messages: int = 500,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Backfill missing namespaces on legacy chat history docs and index messages to RAG.

    Safety properties:
    - Only updates docs where `namespace` is missing/None/blank.
    - Does NOT touch `updated_at` (avoids breaking recency ordering).
    - Reindex uses deterministic IDs (idempotent).
    """

    if not isinstance(user_concept_id, str) or not user_concept_id:
        raise ChatHistoryServiceError("user_concept_id is required")
    if not isinstance(target_namespace, str) or not target_namespace:
        raise ChatHistoryServiceError("target_namespace is required")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    safe_max_sessions = 10
    if isinstance(max_sessions, int) and max_sessions > 0:
        safe_max_sessions = min(max_sessions, 500)

    safe_max_messages = 500
    if isinstance(max_messages, int) and max_messages > 0:
        safe_max_messages = min(max_messages, 100000)

    rag = None
    if get_rag_service:
        try:
            rag = get_rag_service()
        except Exception:
            rag = None

    query = {
        "user_id": user_concept_id,
        "$or": [
            {"namespace": {"$exists": False}},
            {"namespace": {"$eq": None}},
            {"namespace": {"$in": ["", " "]}},
        ],
    }

    sessions_examined = 0
    sessions_updated = 0
    messages_indexed_attempted = 0
    messages_indexed_success = 0
    messages_indexed_failed = 0
    errors: List[Dict[str, Any]] = []

    # Stable namespace UUID for deterministic doc ids.
    deterministic_namespace_uuid = uuid.UUID("8c5a7fa9-9a7c-4f0f-8c1f-f4ad7f9f6fd7")

    try:
        cursor = chat_history_coll.find(query)

        for doc in cursor:
            if sessions_examined >= safe_max_sessions:
                break
            sessions_examined += 1

            session_id = doc.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue

            set_fields: Dict[str, Any] = {"namespace": target_namespace}
            if organisation_concept_id and not doc.get("organisation_concept_id"):
                set_fields["organisation_concept_id"] = organisation_concept_id
            if role_in_org and not doc.get("role_in_org"):
                set_fields["role_in_org"] = role_in_org

            if not dry_run:
                try:
                    chat_history_coll.update_one(
                        {"_id": doc["_id"]}, {"$set": set_fields}
                    )
                    sessions_updated += 1
                except Exception as e:
                    errors.append(
                        {
                            "type": "update_failed",
                            "session_id": session_id,
                            "error": str(e),
                        }
                    )
                    # Continue to next session
                    continue
            else:
                sessions_updated += 1

            # Best-effort reindex
            history = doc.get("history") or []
            if not isinstance(history, list) or not history:
                continue

            for idx, msg in enumerate(history):
                if messages_indexed_attempted >= safe_max_messages:
                    break
                if not isinstance(msg, dict) or _is_reset_marker(msg):
                    continue

                content = msg.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue

                messages_indexed_attempted += 1

                if rag is None or dry_run:
                    messages_indexed_success += 1
                    continue

                try:
                    ts = _coerce_datetime(msg.get("timestamp"))
                    doc_id = str(
                        uuid.uuid5(
                            deterministic_namespace_uuid,
                            f"{target_namespace}|{user_concept_id}|{session_id}|{idx}",
                        )
                    )
                    text = content.strip()
                    if len(text) > 5000:
                        text = text[:5000]

                    metadata = {
                        "type": "chat_message",
                        "user_id": user_concept_id,
                        "session_id": session_id,
                        "role": msg.get("role", "unknown"),
                        "timestamp": ts.isoformat() if ts else None,
                        "organisation_concept_id": organisation_concept_id,
                        "role_in_org": role_in_org,
                    }
                    rag.upsert_documents(
                        [{"id": doc_id, "text": text, "metadata": metadata}],
                        namespace=target_namespace,
                    )
                    messages_indexed_success += 1
                except Exception as e:
                    messages_indexed_failed += 1
                    errors.append(
                        {
                            "type": "index_failed",
                            "session_id": session_id,
                            "message_index": idx,
                            "error": str(e),
                        }
                    )

        return {
            "status": "ok",
            "user_concept_id": user_concept_id,
            "target_namespace": target_namespace,
            "dry_run": bool(dry_run),
            "sessions_examined": sessions_examined,
            "sessions_updated": sessions_updated,
            "messages_indexed_attempted": messages_indexed_attempted,
            "messages_indexed_success": messages_indexed_success,
            "messages_indexed_failed": messages_indexed_failed,
            "errors": errors,
        }
    except PyMongoError as e:
        logger.error(f"Error during chat history backfill: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Backfill failed: {e}") from e


def backfill_chat_history_blob_payloads(
    *,
    user_concept_id: Optional[str] = None,
    namespace: Optional[str] = None,
    session_ids: Optional[List[str]] = None,
    max_sessions: int = 25,
    max_entries: int = 2000,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Backfill oversized inline chat-history payloads into the configured blob store.

    Safety properties:
    - Reuses the existing chat_history.message compaction contract.
    - Never degrades inline payloads to error markers during migration; if blob
      upload fails, the original entry is left untouched and the error is reported.
    - Dry-run reports the candidate rewrites without mutating Mongo.
    - Reruns are safe because existing blob refs are preserved.
    """

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    safe_max_sessions = 25
    if isinstance(max_sessions, int) and max_sessions > 0:
        safe_max_sessions = min(max_sessions, 5000)

    safe_max_entries = 2000
    if isinstance(max_entries, int) and max_entries > 0:
        safe_max_entries = min(max_entries, 500000)

    query: Dict[str, Any] = {}
    if isinstance(user_concept_id, str) and user_concept_id.strip():
        query["user_id"] = user_concept_id.strip()
    if isinstance(namespace, str) and namespace.strip():
        query["namespace"] = namespace.strip()
    if session_ids:
        cleaned_session_ids = [
            str(session_id).strip()
            for session_id in session_ids
            if isinstance(session_id, str) and session_id.strip()
        ]
        if cleaned_session_ids:
            query["session_id"] = {"$in": cleaned_session_ids}

    sessions_examined = 0
    sessions_updated = 0
    entries_examined = 0
    candidate_entries = 0
    entries_updated = 0
    bytes_before = 0
    bytes_after = 0
    limit_reached = False
    errors: List[Dict[str, Any]] = []

    try:
        cursor = chat_history_coll.find(query)

        for doc in cursor:
            if sessions_examined >= safe_max_sessions or limit_reached:
                break
            sessions_examined += 1

            history = doc.get("history") or []
            if not isinstance(history, list) or not history:
                continue

            updated_history: Optional[List[Any]] = None
            session_candidate_entries = 0

            for index, entry in enumerate(history):
                if entries_examined >= safe_max_entries:
                    limit_reached = True
                    break
                if not isinstance(entry, dict):
                    continue

                entries_examined += 1

                debug_payload = entry.get("llm_debug_data")
                request_id_value = (
                    str(debug_payload.get("request_id")).strip()
                    if isinstance(debug_payload, dict)
                    and debug_payload.get("request_id")
                    else None
                )
                entry_namespace = doc.get("namespace")
                compacted = None
                try:
                    compacted = compact_debug_payload_for_storage(
                        entry,
                        root_kind="chat_history.message",
                        namespace=(
                            entry_namespace.strip()
                            if isinstance(entry_namespace, str)
                            and entry_namespace.strip()
                            else None
                        ),
                        request_id=request_id_value,
                        fail_soft=False,
                    )
                except Exception as exc:
                    errors.append(
                        {
                            "type": "entry_offload_failed",
                            "session_id": doc.get("session_id"),
                            "history_index": index,
                            "error": str(exc),
                        }
                    )
                    continue

                compacted_entry = (
                    compacted.payload if isinstance(compacted.payload, dict) else entry
                )
                if compacted_entry == entry:
                    continue

                candidate_entries += 1
                session_candidate_entries += 1
                bytes_before += compacted.original_size_bytes
                bytes_after += compacted.stored_size_bytes

                if updated_history is None:
                    updated_history = list(history)
                updated_history[index] = compacted_entry

            if session_candidate_entries == 0:
                continue

            if dry_run:
                sessions_updated += 1
                entries_updated += session_candidate_entries
                continue

            try:
                chat_history_coll.update_one(
                    {"_id": doc.get("_id")},
                    {"$set": {"history": updated_history}},
                )
                sessions_updated += 1
                entries_updated += session_candidate_entries
            except Exception as exc:
                errors.append(
                    {
                        "type": "session_update_failed",
                        "session_id": doc.get("session_id"),
                        "error": str(exc),
                    }
                )

        return {
            "status": "ok",
            "dry_run": bool(dry_run),
            "query": query,
            "sessions_examined": sessions_examined,
            "sessions_updated": sessions_updated,
            "entries_examined": entries_examined,
            "candidate_entries": candidate_entries,
            "entries_updated": entries_updated,
            "bytes_before": bytes_before,
            "bytes_after": bytes_after,
            "bytes_saved_estimate": max(0, bytes_before - bytes_after),
            "limit_reached": limit_reached,
            "errors": errors,
        }
    except PyMongoError as e:
        logger.error(
            f"Error during chat history blob payload backfill: {e}", exc_info=True
        )
        raise ChatHistoryServiceError(
            f"Chat history blob payload backfill failed: {e}"
        ) from e


def reindex_chat_history_for_user_namespace(
    *,
    user_concept_id: str,
    target_namespace: str,
    organisation_concept_id: Optional[str] = None,
    role_in_org: Optional[str] = None,
    session_ids: Optional[List[str]] = None,
    max_sessions: int = 50,
    max_messages: int = 5000,
    reset_counters: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Reindex chat history messages into RAG for sessions already in a namespace.

    This exists because older chat sessions may have:
    - never been indexed (RAG not available at the time), or
    - been indexed with non-deterministic IDs (hard to deduplicate), or
    - missing/incorrect per-session counters.

    Behaviour:
    - Scopes to documents with {user_id=user_concept_id, namespace=target_namespace}
    - Indexes only non-reset messages with non-empty string content
    - Uses deterministic IDs (idempotent) so repeated runs are safe
    - Optionally resets rag_indexed_success/failed per session before reindexing
    """

    if not isinstance(user_concept_id, str) or not user_concept_id:
        raise ChatHistoryServiceError("user_concept_id is required")
    if not isinstance(target_namespace, str) or not target_namespace:
        raise ChatHistoryServiceError("target_namespace is required")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    safe_max_sessions = 50
    if isinstance(max_sessions, int) and max_sessions > 0:
        safe_max_sessions = min(max_sessions, 2000)

    safe_max_messages = 5000
    if isinstance(max_messages, int) and max_messages > 0:
        safe_max_messages = min(max_messages, 200000)

    rag = None
    if get_rag_service:
        try:
            rag = get_rag_service()
        except Exception:
            rag = None

    query: Dict[str, Any] = {
        "user_id": user_concept_id,
        "namespace": target_namespace,
    }
    if session_ids:
        query["session_id"] = {"$in": [sid for sid in session_ids if sid]}

    sessions_examined = 0
    sessions_reindexed = 0
    sessions_skipped = 0
    messages_indexed_attempted = 0
    messages_indexed_success = 0
    messages_indexed_failed = 0
    errors: List[Dict[str, Any]] = []

    deterministic_namespace_uuid = uuid.UUID("8c5a7fa9-9a7c-4f0f-8c1f-f4ad7f9f6fd7")

    try:
        cursor = chat_history_coll.find(query)

        for doc in cursor:
            if sessions_examined >= safe_max_sessions:
                break
            sessions_examined += 1

            session_id = doc.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue

            history = doc.get("history") or []
            if not isinstance(history, list) or not history:
                continue

            indexable_total, signature = _compute_rag_history_signature(history)
            if _should_skip_rag_reindex(doc, signature, indexable_total):
                sessions_skipped += 1
                continue

            if reset_counters and not dry_run:
                try:
                    chat_history_coll.update_one(
                        {"_id": doc["_id"]},
                        {"$set": {"rag_indexed_success": 0, "rag_indexed_failed": 0}},
                    )
                except Exception:
                    # Best-effort only.
                    pass

            did_any = False
            session_success = 0
            session_failed = 0
            for idx, msg in enumerate(history):
                if messages_indexed_attempted >= safe_max_messages:
                    break
                if not isinstance(msg, dict) or _is_reset_marker(msg):
                    continue

                content = msg.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue

                messages_indexed_attempted += 1
                did_any = True

                if rag is None or dry_run:
                    messages_indexed_success += 1
                    session_success += 1
                    continue

                try:
                    ts = _coerce_datetime(msg.get("timestamp"))
                    doc_id = str(
                        uuid.uuid5(
                            deterministic_namespace_uuid,
                            f"{target_namespace}|{user_concept_id}|{session_id}|{idx}",
                        )
                    )
                    text = content.strip()
                    if len(text) > 5000:
                        text = text[:5000]

                    metadata = {
                        "type": "chat_message",
                        "user_id": user_concept_id,
                        "session_id": session_id,
                        "role": msg.get("role", "unknown"),
                        "timestamp": ts.isoformat() if ts else None,
                        "organisation_concept_id": organisation_concept_id,
                        "role_in_org": role_in_org,
                        "reindexed": True,
                    }

                    rag.upsert_documents(
                        [{"id": doc_id, "text": text, "metadata": metadata}],
                        namespace=target_namespace,
                    )
                    messages_indexed_success += 1
                    session_success += 1

                    try:
                        chat_history_coll.update_one(
                            {"user_id": user_concept_id, "session_id": session_id},
                            {"$inc": {"rag_indexed_success": 1}},
                        )
                    except Exception:
                        pass
                except Exception as e:
                    messages_indexed_failed += 1
                    session_failed += 1
                    errors.append(
                        {
                            "type": "index_failed",
                            "session_id": session_id,
                            "message_index": idx,
                            "error": str(e),
                        }
                    )
                    try:
                        chat_history_coll.update_one(
                            {"user_id": user_concept_id, "session_id": session_id},
                            {"$inc": {"rag_indexed_failed": 1}},
                        )
                    except Exception:
                        pass

            if did_any:
                sessions_reindexed += 1
                if (
                    rag is not None
                    and not dry_run
                    and session_failed == 0
                    and session_success >= indexable_total
                ):
                    _update_rag_history_signature(
                        chat_history_coll,
                        doc.get("_id"),
                        signature,
                        indexable_total,
                    )

        return {
            "status": "ok",
            "user_concept_id": user_concept_id,
            "target_namespace": target_namespace,
            "dry_run": bool(dry_run),
            "reset_counters": bool(reset_counters),
            "sessions_examined": sessions_examined,
            "sessions_reindexed": sessions_reindexed,
            "sessions_skipped": sessions_skipped,
            "messages_indexed_attempted": messages_indexed_attempted,
            "messages_indexed_success": messages_indexed_success,
            "messages_indexed_failed": messages_indexed_failed,
            "errors": errors,
        }
    except PyMongoError as e:
        logger.error(f"Error during chat history reindex: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Reindex failed: {e}") from e


def reindex_chat_history_session_chunk(
    *,
    user_concept_id: str,
    target_namespace: str,
    session_id: str,
    organisation_concept_id: Optional[str] = None,
    role_in_org: Optional[str] = None,
    chunk_start: int = 0,
    chunk_size: int = 25,
    reset_counters: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Reindex a single chat history session in small chunks.

    This exists to make long-running reindex operations reliable:
    - Each call processes at most `chunk_size` indexable messages.
    - Returns `next_chunk_start` so the client can resume.

    `chunk_start` is an index into the raw `history` list (not filtered).
    This keeps resumption stable across calls.
    """

    if not isinstance(user_concept_id, str) or not user_concept_id:
        raise ChatHistoryServiceError("user_concept_id is required")
    if not isinstance(target_namespace, str) or not target_namespace:
        raise ChatHistoryServiceError("target_namespace is required")
    if not isinstance(session_id, str) or not session_id:
        raise ChatHistoryServiceError("session_id is required")

    safe_chunk_start = 0
    if isinstance(chunk_start, int) and chunk_start > 0:
        safe_chunk_start = min(chunk_start, 1_000_000)

    safe_chunk_size = 25
    if isinstance(chunk_size, int) and chunk_size > 0:
        safe_chunk_size = min(chunk_size, 500)

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    rag = None
    if get_rag_service:
        try:
            rag = get_rag_service()
        except Exception:
            rag = None

    doc = chat_history_coll.find_one(
        {
            "user_id": user_concept_id,
            "namespace": target_namespace,
            "session_id": session_id,
        },
        {
            "history": 1,
            "_id": 1,
            "rag_history_signature": 1,
            "rag_indexed_success": 1,
            "rag_indexed_failed": 1,
        },
    )
    if not doc:
        return {
            "status": "not_found",
            "user_concept_id": user_concept_id,
            "target_namespace": target_namespace,
            "session_id": session_id,
            "chunk_start": safe_chunk_start,
            "chunk_size": safe_chunk_size,
            "done": True,
        }

    history = doc.get("history") or []
    if not isinstance(history, list) or not history:
        return {
            "status": "ok",
            "user_concept_id": user_concept_id,
            "target_namespace": target_namespace,
            "session_id": session_id,
            "chunk_start": safe_chunk_start,
            "chunk_size": safe_chunk_size,
            "history_len": 0,
            "indexable_total": 0,
            "messages_indexed_attempted": 0,
            "messages_indexed_success": 0,
            "messages_indexed_failed": 0,
            "errors": [],
            "next_chunk_start": 0,
            "done": True,
        }

    history_len = len(history)
    indexable_total, signature = _compute_rag_history_signature(history)

    if safe_chunk_start == 0 and _should_skip_rag_reindex(
        doc, signature, indexable_total
    ):
        return {
            "status": "ok",
            "user_concept_id": user_concept_id,
            "target_namespace": target_namespace,
            "session_id": session_id,
            "chunk_start": safe_chunk_start,
            "chunk_size": safe_chunk_size,
            "history_len": history_len,
            "indexable_total": indexable_total,
            "messages_indexed_attempted": 0,
            "messages_indexed_success": 0,
            "messages_indexed_failed": 0,
            "errors": [],
            "next_chunk_start": history_len,
            "done": True,
            "skipped": True,
        }

    if reset_counters and not dry_run:
        try:
            chat_history_coll.update_one(
                {"_id": doc["_id"]},
                {"$set": {"rag_indexed_success": 0, "rag_indexed_failed": 0}},
            )
        except Exception:
            pass

    deterministic_namespace_uuid = uuid.UUID("8c5a7fa9-9a7c-4f0f-8c1f-f4ad7f9f6fd7")
    docs_to_upsert: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    next_chunk_start = history_len
    for idx in range(safe_chunk_start, history_len):
        msg = history[idx]
        if not isinstance(msg, dict) or _is_reset_marker(msg):
            continue

        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue

        ts = _coerce_datetime(msg.get("timestamp"))
        doc_id = str(
            uuid.uuid5(
                deterministic_namespace_uuid,
                f"{target_namespace}|{user_concept_id}|{session_id}|{idx}",
            )
        )
        text = content.strip()
        if len(text) > 5000:
            text = text[:5000]

        metadata = {
            "type": "chat_message",
            "user_id": user_concept_id,
            "session_id": session_id,
            "role": msg.get("role", "unknown"),
            "timestamp": ts.isoformat() if ts else None,
            "organisation_concept_id": organisation_concept_id,
            "role_in_org": role_in_org,
            "reindexed": True,
        }

        docs_to_upsert.append({"id": doc_id, "text": text, "metadata": metadata})

        if len(docs_to_upsert) >= safe_chunk_size:
            next_chunk_start = idx + 1
            break

    if not docs_to_upsert:
        return {
            "status": "ok",
            "user_concept_id": user_concept_id,
            "target_namespace": target_namespace,
            "session_id": session_id,
            "chunk_start": safe_chunk_start,
            "chunk_size": safe_chunk_size,
            "history_len": history_len,
            "indexable_total": indexable_total,
            "messages_indexed_attempted": 0,
            "messages_indexed_success": 0,
            "messages_indexed_failed": 0,
            "errors": [],
            "next_chunk_start": history_len,
            "done": True,
        }

    messages_indexed_attempted = len(docs_to_upsert)
    messages_indexed_success = 0
    messages_indexed_failed = 0

    if rag is None or dry_run:
        messages_indexed_success = messages_indexed_attempted
    else:
        try:
            s, f = rag.upsert_documents(
                docs_to_upsert,
                namespace=target_namespace,
                allow_partial_failures=True,
            )
            messages_indexed_success += int(s or 0)
            messages_indexed_failed += int(f or 0)
        except Exception as e:
            # Best-effort: treat the whole chunk as failed.
            messages_indexed_failed += messages_indexed_attempted
            errors.append(
                {
                    "type": "index_failed",
                    "session_id": session_id,
                    "chunk_start": safe_chunk_start,
                    "chunk_size": safe_chunk_size,
                    "error": str(e),
                }
            )

    if not dry_run:
        try:
            if messages_indexed_success:
                chat_history_coll.update_one(
                    {"user_id": user_concept_id, "session_id": session_id},
                    {"$inc": {"rag_indexed_success": messages_indexed_success}},
                )
            if messages_indexed_failed:
                chat_history_coll.update_one(
                    {"user_id": user_concept_id, "session_id": session_id},
                    {"$inc": {"rag_indexed_failed": messages_indexed_failed}},
                )
        except Exception:
            pass

    done = next_chunk_start >= history_len
    if (
        done
        and rag is not None
        and not dry_run
        and messages_indexed_failed == 0
        and signature
    ):
        previous_success = 0
        previous_failed = 0
        if not reset_counters:
            try:
                previous_success = int(doc.get("rag_indexed_success") or 0)
            except (TypeError, ValueError):
                previous_success = 0
            try:
                previous_failed = int(doc.get("rag_indexed_failed") or 0)
            except (TypeError, ValueError):
                previous_failed = 0
        if (
            previous_failed == 0
            and (previous_success + messages_indexed_success) >= indexable_total
        ):
            _update_rag_history_signature(
                chat_history_coll,
                doc.get("_id"),
                signature,
                indexable_total,
            )

    return {
        "status": "ok",
        "user_concept_id": user_concept_id,
        "target_namespace": target_namespace,
        "session_id": session_id,
        "chunk_start": safe_chunk_start,
        "chunk_size": safe_chunk_size,
        "history_len": history_len,
        "indexable_total": indexable_total,
        "messages_indexed_attempted": messages_indexed_attempted,
        "messages_indexed_success": messages_indexed_success,
        "messages_indexed_failed": messages_indexed_failed,
        "errors": errors,
        "next_chunk_start": next_chunk_start,
        "done": done,
    }
