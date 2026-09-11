"""Chat history service for persistent conversation storage."""

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Iterable, Mapping
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import (
    DuplicateKeyError,
    OperationFailure,
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
_SESSION_CONTEXT_UNSET = object()
CONVERSATION_SITUATION_TEXT_MAX_CHARS = 12_000
_CONVERSATION_SITUATION_SOURCE_MAX_CHARS = 120
_CONVERSATION_SITUATION_UPDATER_MAX_CHARS = 160
_CONVERSATION_SITUATION_REQUEST_ID_MAX_CHARS = 160
CONVERSATION_OBSERVATION_MAX_ITEMS = 12
_CONVERSATION_OBSERVATION_ID_MAX_CHARS = 128
_CONVERSATION_OBSERVATION_KIND_MAX_CHARS = 80
_CONVERSATION_OBSERVATION_VALUE_MAX_CHARS = 1_000
_CHAT_HISTORY_INDEXES_READY = False
_CHAT_HISTORY_INDEXES_LOCK = threading.Lock()
_CHAT_HISTORY_READ_CIRCUIT_LOCK = threading.Lock()
_CHAT_HISTORY_READ_CIRCUIT_UNTIL_MONOTONIC = 0.0
_CHAT_HISTORY_READ_CIRCUIT_LAST_ERROR: Optional[str] = None
_CHAT_HISTORY_LIGHT_SESSION_METADATA_INDEX_NAME = (
    "namespace_user_recency_session_metadata_v1"
)
CONVERSATION_SEARCH_INDEX_VERSION = 1
_CONVERSATION_SEARCH_TEXT_INDEX_NAME = "conversation_search_text_v1"
_CONCEPT_QA_ACTIVE_KEY_INDEX_NAME = "concept_q_and_a_active_key_unique_v1"
_CONVERSATION_SEARCH_MAX_SEGMENT_CHARS = 5_000
_MAX_FOCAL_CONCEPT_IDS = 4
HISTORY_LLM_EXECUTION_SUMMARY_SCHEMA_VERSION = "history_llm_execution_summary.v1"
_HISTORY_LLM_EXECUTION_SUMMARY_STRING_MAX_CHARS = 240
CHAT_SESSION_ORIGIN_KIND_BROWSER_TEST_FIXTURE = "browser_test_fixture"
CHAT_SESSION_ORIGIN_KIND_BENCHMARK_HARNESS = "benchmark_harness"
CHAT_SESSION_ORIGIN_KIND_CODING_AGENT_TEST = "coding_agent_test"
CHAT_SESSION_ORIGIN_KIND_CONCEPT_Q_AND_A = "concept_q_and_a"
CHAT_SESSION_MODE_CONCEPT_Q_AND_A = "concept_q_and_a"
CHAT_SESSION_AGENT_CREATED_ORIGIN_KINDS = frozenset(
    {
        CHAT_SESSION_ORIGIN_KIND_BROWSER_TEST_FIXTURE,
        CHAT_SESSION_ORIGIN_KIND_BENCHMARK_HARNESS,
        CHAT_SESSION_ORIGIN_KIND_CODING_AGENT_TEST,
        "coding_agent",
        "agent_test",
    }
)


def _bounded_history_llm_execution_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:_HISTORY_LLM_EXECUTION_SUMMARY_STRING_MAX_CHARS]


def build_history_llm_execution_summary(
    llm_debug_data: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Project only safe model and credential provenance needed after reload.

    The full debug payload can contain prompts, tool results, and other material
    that normal history deliberately omits.  This projection is an explicit
    allow-list and must remain small enough to return with ordinary history.
    """

    if not isinstance(llm_debug_data, Mapping):
        return None
    raw_interaction = llm_debug_data.get("llm_interaction")
    if not isinstance(raw_interaction, Mapping):
        return None

    projected_calls: list[dict[str, Any]] = []
    raw_calls = raw_interaction.get("calls")
    if isinstance(raw_calls, list):
        for raw_call in raw_calls:
            if not isinstance(raw_call, Mapping):
                continue
            projected_call: dict[str, Any] = {}
            for field_name in ("type", "model", "provider", "stage", "status"):
                clean_value = _bounded_history_llm_execution_string(
                    raw_call.get(field_name)
                )
                if clean_value is not None:
                    projected_call[field_name] = clean_value
            if isinstance(raw_call.get("success"), bool):
                projected_call["success"] = raw_call["success"]

            transport_candidates = (
                raw_call.get("transport"),
                raw_call.get("transport_metadata"),
                (
                    raw_call.get("candidate", {}).get("transport_metadata")
                    if isinstance(raw_call.get("candidate"), Mapping)
                    else None
                ),
                (
                    raw_call.get("candidate", {}).get("llm_transport")
                    if isinstance(raw_call.get("candidate"), Mapping)
                    else None
                ),
                (
                    raw_call.get("metadata", {}).get("transport_metadata")
                    if isinstance(raw_call.get("metadata"), Mapping)
                    else None
                ),
            )
            projected_transport: dict[str, Any] = {}
            for raw_transport in transport_candidates:
                if not isinstance(raw_transport, Mapping):
                    continue
                source = _bounded_history_llm_execution_string(
                    raw_transport.get("credential_source")
                )
                if source and source.lower() in {"primary", "backup"}:
                    projected_transport["credential_source"] = source.lower()
                if isinstance(raw_transport.get("credential_failover_used"), bool):
                    projected_transport["credential_failover_used"] = raw_transport[
                        "credential_failover_used"
                    ]
                failure_kind = _bounded_history_llm_execution_string(
                    raw_transport.get("primary_credential_failure_kind")
                )
                if failure_kind is not None:
                    projected_transport["primary_credential_failure_kind"] = (
                        failure_kind
                    )
            if projected_transport:
                projected_call["transport"] = projected_transport
            if projected_call:
                projected_calls.append(projected_call)

    projected_interaction: dict[str, Any] = {"calls": projected_calls}
    for field_name in (
        "requested_model",
        "requested_provider",
        "ordinary_turn_terminal_status",
    ):
        clean_value = _bounded_history_llm_execution_string(
            raw_interaction.get(field_name)
        )
        if clean_value is not None:
            projected_interaction[field_name] = clean_value

    top_level_model = _bounded_history_llm_execution_string(llm_debug_data.get("model"))
    if not projected_calls and len(projected_interaction) == 1 and not top_level_model:
        return None

    summary: dict[str, Any] = {
        "schema_version": HISTORY_LLM_EXECUTION_SUMMARY_SCHEMA_VERSION,
        "llm_interaction": projected_interaction,
    }
    if top_level_model is not None:
        summary["model"] = top_level_model
    timestamp = _bounded_history_llm_execution_string(
        llm_debug_data.get("timestamp")
        or llm_debug_data.get("interaction_timestamp_utc")
    )
    if timestamp is not None:
        summary["timestamp"] = timestamp
    return summary


CHAT_SESSION_PROVENANCE_FIELDS = (
    "mode",
    "origin_kind",
    "created_by_actor_concept_id",
    "created_by_actor_type",
    "is_agent_created",
    "test_artifact_kind",
)
EXTERNAL_CONVERSATION_IMPORT_FIELD = "external_conversation_import"
EXTERNAL_CONVERSATION_IMPORT_SCHEMA_VERSION = "external_conversation_import.v1"
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


def _chat_history_read_circuit_seconds() -> float:
    """Return how long a recent read failure remains visible as an advisory."""

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


def find_chat_history_document_for_read(
    chat_history_coll,
    query: Dict[str, Any],
    projection: Optional[Dict[str, Any]] = None,
):
    """Run an observed read-only find_one against chat_history."""
    return _read_find_one(chat_history_coll, query, projection)


def _current_chat_history_read_circuit_remaining_seconds() -> float:
    now = time.monotonic()
    with _CHAT_HISTORY_READ_CIRCUIT_LOCK:
        return max(0.0, _CHAT_HISTORY_READ_CIRCUIT_UNTIL_MONOTONIC - now)


def _guard_chat_history_read(op_name: str) -> None:
    """Surface recent read degradation without rejecting a fresh read.

    The former circuit breaker treated a transient failure as grounds to reject
    all canonical reads for several seconds.  That made availability worse and
    could hide a recovery that had already happened.  Keep the short-lived
    state as operator-facing context, but let each authorised read attempt
    proceed and reconcile its own result.
    """

    remaining = _current_chat_history_read_circuit_remaining_seconds()
    if remaining <= 0:
        return
    with _CHAT_HISTORY_READ_CIRCUIT_LOCK:
        last_error = _CHAT_HISTORY_READ_CIRCUIT_LAST_ERROR
    detail = f"; last_error={last_error}" if last_error else ""
    logger.warning(
        "Chat history read advisory active for %s (%.1fs remaining%s); "
        "continuing canonical read.",
        op_name,
        remaining,
        detail,
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
        "Marking chat history reads degraded for %.1fs after %s failure; "
        "subsequent reads will still proceed: %s",
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
    kwargs: Dict[str, Any] = {}
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
    except (TypeError, OperationFailure) as exc:
        # Older Mongo-compatible stores and test doubles may not accept the
        # observability comment option. Retry only that transport mismatch.
        if isinstance(exc, OperationFailure):
            message = str(exc).lower()
            unsupported_comment = "comment" in message and any(
                marker in message
                for marker in (
                    "unrecognized field",
                    "unrecognised field",
                    "unknown option",
                )
            )
            if not unsupported_comment:
                raise
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
    except (TypeError, OperationFailure) as exc:
        # Test doubles and older compatible servers may not accept comment kwargs.
        if isinstance(exc, OperationFailure):
            message = str(exc).lower()
            unsupported_comment = "comment" in message and any(
                marker in message
                for marker in ("unrecognized", "unknown", "unsupported", "invalid")
            )
            if not unsupported_comment:
                raise
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
    return cursor


def _read_aggregate(
    chat_history_coll,
    pipeline: List[Dict[str, Any]],
    *,
    operation: str = "aggregate",
    detail: Optional[str] = None,
):
    kwargs: Dict[str, Any] = {}
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
        # Test doubles may not accept PyMongo comment kwargs.
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
            if (
                "namespace_1_user_id_1_focal_concept_ids_1_updated_at_-1"
                not in existing_indexes
            ):
                collection.create_index(
                    [
                        ("namespace", ASCENDING),
                        ("user_id", ASCENDING),
                        ("focal_concept_ids", ASCENDING),
                        ("updated_at", DESCENDING),
                    ],
                    name="namespace_1_user_id_1_focal_concept_ids_1_updated_at_-1",
                )
            if _CONCEPT_QA_ACTIVE_KEY_INDEX_NAME not in existing_indexes:
                collection.create_index(
                    [("concept_q_and_a.active_key", ASCENDING)],
                    name=_CONCEPT_QA_ACTIVE_KEY_INDEX_NAME,
                    unique=True,
                    partialFilterExpression={
                        "mode": CHAT_SESSION_MODE_CONCEPT_Q_AND_A,
                        "origin_kind": CHAT_SESSION_ORIGIN_KIND_CONCEPT_Q_AND_A,
                        "concept_q_and_a.lifecycle.status": "active",
                        "concept_q_and_a.active_key": {"$type": "string"},
                    },
                )
            if _CONVERSATION_SEARCH_TEXT_INDEX_NAME not in existing_indexes:
                collection.create_index(
                    [
                        ("namespace", ASCENDING),
                        ("user_id", ASCENDING),
                        ("conversation_search_title", "text"),
                        ("conversation_search_text", "text"),
                    ],
                    name=_CONVERSATION_SEARCH_TEXT_INDEX_NAME,
                    weights={
                        "conversation_search_title": 8,
                        "conversation_search_text": 1,
                    },
                    default_language="none",
                )
            if "namespace_1_user_id_1_trashed_at_1_updated_at_-1" not in existing_indexes:
                collection.create_index(
                    [
                        ("namespace", ASCENDING),
                        ("user_id", ASCENDING),
                        ("trashed_at", ASCENDING),
                        ("updated_at", DESCENDING),
                    ],
                    name="namespace_1_user_id_1_trashed_at_1_updated_at_-1",
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


def _conversation_search_message_text(message: Mapping[str, Any]) -> Optional[str]:
    """Return only user-visible conversational text for the lexical projection."""

    role = message.get("role")
    if role not in {"user", "assistant"}:
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content.strip()[:_CONVERSATION_SEARCH_MAX_SEGMENT_CHARS]


def _conversation_search_segment(
    message: Mapping[str, Any], *, history_index: int | None = None
) -> Optional[Dict[str, Any]]:
    """Build a safe searchable segment with a stable source locator."""

    text = _conversation_search_message_text(message)
    if text is None:
        return None
    timestamp = message.get("timestamp")
    if isinstance(timestamp, datetime):
        timestamp = timestamp.isoformat()
    elif not isinstance(timestamp, str):
        timestamp = None
    message_id = next(
        (
            value.strip()
            for key in ("message_id", "turn_id", "request_id", "id")
            for value in [message.get(key)]
            if isinstance(value, str) and value.strip()
        ),
        None,
    )
    locator = {
        key: value
        for key, value in {
            "message_id": message_id,
            "timestamp": timestamp,
            "history_index": history_index,
            "role": message.get("role"),
        }.items()
        if value is not None
    }
    return {
        "text": text,
        "role": message.get("role"),
        "source_locator": locator,
    }


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
    mode = _normalise_session_provenance_text(
        doc.get("mode"),
        max_len=80,
        identifier=True,
    )
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
        "mode": mode,
        "origin_kind": origin_kind,
        "created_by_actor_concept_id": actor_id,
        "created_by_actor_type": actor_type,
        "is_agent_created": is_agent_created,
        "test_artifact_kind": test_kind,
    }


def project_concept_q_and_a_session_metadata(
    doc: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return the bounded mode/lifecycle carrier needed by generic chat views."""

    raw_state = doc.get("concept_q_and_a")
    if not isinstance(raw_state, Mapping):
        return None
    raw_concept = raw_state.get("concept")
    concept = (
        {
            key: raw_concept.get(key)
            for key in ("concept_id", "name", "storage_id", "mongo_id")
            if raw_concept.get(key) is not None
        }
        if isinstance(raw_concept, Mapping)
        else None
    )
    raw_lifecycle = raw_state.get("lifecycle")
    lifecycle: Dict[str, Any] = {}
    if isinstance(raw_lifecycle, Mapping):
        for key in ("status", "revision", "terminal_reason"):
            if raw_lifecycle.get(key) is not None:
                lifecycle[key] = raw_lifecycle.get(key)
        for key in (
            "started_at",
            "updated_at",
            "terminal_at",
            "finished_at",
            "cancelled_at",
        ):
            value = raw_lifecycle.get(key)
            timestamp = _coerce_datetime(value)
            if timestamp is not None:
                lifecycle[key] = timestamp.isoformat()
            elif isinstance(value, str) and value.strip():
                lifecycle[key] = value.strip()
    raw_initial_question = raw_state.get("initial_question")
    initial_question: Dict[str, Any] = {}
    if isinstance(raw_initial_question, Mapping):
        status = raw_initial_question.get("status")
        if isinstance(status, str) and status.strip():
            initial_question["status"] = status.strip()
        retryable = raw_initial_question.get("retryable")
        if isinstance(retryable, bool):
            initial_question["retryable"] = retryable
        attempt_count = raw_initial_question.get("attempt_count")
        if (
            isinstance(attempt_count, int)
            and not isinstance(attempt_count, bool)
            and attempt_count >= 0
        ):
            initial_question["attempt_count"] = attempt_count
        last_attempt_at = raw_initial_question.get("last_attempt_at")
        timestamp = _coerce_datetime(last_attempt_at)
        if timestamp is not None:
            initial_question["last_attempt_at"] = timestamp.isoformat()
        elif isinstance(last_attempt_at, str) and last_attempt_at.strip():
            initial_question["last_attempt_at"] = last_attempt_at.strip()
        raw_failure = raw_initial_question.get("failure")
        if isinstance(raw_failure, Mapping):
            failure: Dict[str, Any] = {}
            error_code = raw_failure.get("error_code")
            if isinstance(error_code, str) and error_code.strip():
                failure["error_code"] = error_code.strip()
            message = raw_failure.get("message")
            if isinstance(message, str) and message.strip():
                failure["message"] = message.strip()
            if failure:
                initial_question["failure"] = failure
    projection = {
        "schema_version": raw_state.get("schema_version"),
        "concept": concept,
        "lifecycle": lifecycle or None,
    }
    if initial_question:
        projection["initial_question"] = initial_question
    return projection


def _concept_q_and_a_metadata_field(doc: Mapping[str, Any]) -> Dict[str, Any]:
    projected = project_concept_q_and_a_session_metadata(doc)
    return {"concept_q_and_a": projected} if projected is not None else {}


def _concept_q_and_a_completion(
    doc: Mapping[str, Any],
) -> tuple[bool, Optional[datetime]]:
    projected = project_concept_q_and_a_session_metadata(doc)
    lifecycle = projected.get("lifecycle") if isinstance(projected, Mapping) else None
    if not isinstance(lifecycle, Mapping) or lifecycle.get("status") not in {
        "finished",
        "cancelled",
    }:
        return False, None
    completed_at = _coerce_datetime(
        lifecycle.get("terminal_at")
        or lifecycle.get("finished_at")
        or lifecycle.get("cancelled_at")
    )
    return True, completed_at


def project_chat_session_mode_state(doc: Mapping[str, Any]) -> Dict[str, Any]:
    """Project the generic session fields needed to reopen a specialised carrier."""

    projection: Dict[str, Any] = {
        "session_id": doc.get("session_id"),
        **_session_provenance_from_doc(dict(doc)),
        **_focus_projection_from_doc(dict(doc)),
    }
    concept_q_and_a = project_concept_q_and_a_session_metadata(doc)
    if concept_q_and_a is not None:
        projection["concept_q_and_a"] = concept_q_and_a
        projection["concept"] = concept_q_and_a.get("concept")
        projection["lifecycle"] = concept_q_and_a.get("lifecycle")
        projection["initial_question"] = concept_q_and_a.get("initial_question")
    return projection


def _external_conversation_summary_from_doc(
    doc: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return bounded imported-conversation metadata safe for actor-scoped UI reads."""

    manifest = doc.get(EXTERNAL_CONVERSATION_IMPORT_FIELD)
    if not isinstance(manifest, Mapping):
        return None
    if manifest.get("schema_version") != EXTERNAL_CONVERSATION_IMPORT_SCHEMA_VERSION:
        return None
    loss_report = manifest.get("loss_report")
    safe_loss_report: Dict[str, Any] = {}
    if isinstance(loss_report, Mapping):
        for key in (
            "total_event_count",
            "user_visible_event_count",
            "non_projected_event_count",
            "projected_message_count",
            "projected_character_count",
            "truncated_message_count",
            "truncated_character_count",
            "omitted_visible_message_count",
            "projection_complete",
            "invalid_or_unsupported_record_count",
            "instruction_event_count",
            "sidechain_record_count",
            "hidden_request_count",
            "journal_record_count",
            "assistant_message_count",
            "source_has_no_assistant_messages",
            "oversized_record_count",
            "branch_information_present",
        ):
            value = loss_report.get(key)
            if isinstance(value, (bool, int)) and not isinstance(value, float):
                safe_loss_report[key] = value
    synchronisation = manifest.get("synchronisation")
    safe_synchronisation = None
    if isinstance(synchronisation, Mapping):
        divergence = synchronisation.get("divergence")
        safe_divergence = None
        if isinstance(divergence, Mapping):
            safe_divergence = {
                key: divergence.get(key)
                for key in (
                    "schema_version",
                    "detected_at_utc",
                    "missing_event_count",
                    "changed_event_count",
                    "reordered",
                    "existing_history_preserved",
                )
            }
        safe_synchronisation = {
            "schema_version": synchronisation.get("schema_version"),
            "action": synchronisation.get("action"),
            "checked_at_utc": synchronisation.get("checked_at_utc"),
            "appended_message_count": synchronisation.get("appended_message_count"),
            "retained_message_count": synchronisation.get("retained_message_count"),
            "divergence": safe_divergence,
        }
    return {
        "schema_version": manifest.get("schema_version"),
        "read_only": manifest.get("read_only") is True,
        "provider": _normalise_session_provenance_text(
            manifest.get("provider"), max_len=80, identifier=True
        ),
        "source_session_id": _normalise_session_provenance_text(
            manifest.get("source_session_id"), max_len=240
        ),
        "source_title": _normalise_session_provenance_text(
            manifest.get("source_title"), max_len=160
        ),
        "source_format": _normalise_session_provenance_text(
            manifest.get("source_format"), max_len=80, identifier=True
        ),
        "source_sha256": _normalise_session_provenance_text(
            manifest.get("source_sha256"), max_len=64
        ),
        "package_sha256": _normalise_session_provenance_text(
            manifest.get("package_sha256"), max_len=64
        ),
        "parser_id": _normalise_session_provenance_text(
            manifest.get("parser_id"), max_len=120
        ),
        "parser_version": _normalise_session_provenance_text(
            manifest.get("parser_version"), max_len=80
        ),
        "source_created_at_utc": manifest.get("source_created_at_utc"),
        "source_updated_at_utc": manifest.get("source_updated_at_utc"),
        "imported_at_utc": manifest.get("imported_at_utc"),
        "participant_count": manifest.get("participant_count"),
        "event_count": manifest.get("event_count"),
        "loss_report": safe_loss_report,
        "synchronisation": safe_synchronisation,
    }


def _conversation_lineage_summary_from_doc(
    doc: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    lineage = doc.get("conversation_lineage")
    if not isinstance(lineage, Mapping):
        return None
    allowed = (
        "schema_version",
        "lineage_kind",
        "forked_from_session_id",
        "source_provider",
        "source_session_id",
        "source_snapshot_sha256",
        "source_package_sha256",
        "created_at_utc",
    )
    return {key: lineage.get(key) for key in allowed if lineage.get(key) is not None}


def _session_external_projection_from_doc(doc: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "external_conversation": _external_conversation_summary_from_doc(doc),
        "conversation_lineage": _conversation_lineage_summary_from_doc(doc),
    }


def project_external_conversation_metadata(
    doc: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Project bounded external-import metadata from an actor-authorised document."""

    return _external_conversation_summary_from_doc(doc)


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
    projection[EXTERNAL_CONVERSATION_IMPORT_FIELD] = 1
    projection["conversation_lineage"] = 1
    projection["concept_q_and_a.schema_version"] = 1
    projection["concept_q_and_a.concept"] = 1
    projection["concept_q_and_a.lifecycle"] = 1
    projection["concept_q_and_a.initial_question"] = 1
    return projection


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
            applied_prompt_snapshot=(
                llm_debug_data.get("applied_prompt_snapshot")
                if isinstance(llm_debug_data.get("applied_prompt_snapshot"), dict)
                else None
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


class ConversationActiveWorkError(ChatHistoryServiceError):
    """Raised when recoverable Trash is blocked by queued or running work."""


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


def _isoformat_datetime(value: Any) -> Optional[str]:
    parsed = _coerce_datetime(value)
    return parsed.isoformat() if parsed is not None else None


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
        if isinstance(entry.get("image_attachments"), list):
            copied["image_attachments"] = list(entry["image_attachments"])
        turn_id = entry.get("turn_id")
        if isinstance(turn_id, str) and turn_id.strip():
            copied["turn_id"] = turn_id.strip()
        author_user_id = entry.get("author_user_id")
        if not isinstance(author_user_id, str) or not author_user_id.strip():
            author_user_id = None
        if not author_user_id and entry.get("role") == "user":
            if isinstance(owner_user_id, str) and owner_user_id.strip():
                author_user_id = owner_user_id
        if author_user_id:
            copied["author_user_id"] = author_user_id
        llm_execution_summary = entry.get("llm_execution_summary")
        if isinstance(llm_execution_summary, Mapping):
            copied["llm_execution_summary"] = dict(llm_execution_summary)
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


def _normalise_conversation_situation_metadata(
    value: Any,
    *,
    field_name: str,
    max_chars: int,
    required: bool,
) -> Optional[str]:
    if not isinstance(value, str):
        if required:
            raise ChatHistoryServiceError(f"{field_name} is required.")
        return None
    cleaned = value.strip()
    if not cleaned:
        if required:
            raise ChatHistoryServiceError(f"{field_name} is required.")
        return None
    if len(cleaned) > max_chars:
        raise ChatHistoryServiceError(
            f"{field_name} must be {max_chars} characters or fewer."
        )
    return cleaned


def _normalise_conversation_situation_revision(
    value: Any,
    *,
    field_name: str = "expected_revision",
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ChatHistoryServiceError(f"{field_name} must be a non-negative integer.")
    return value


def _conversation_situation_from_doc(
    doc: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    raw = doc.get("conversation_situation")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ChatHistoryServiceError(
            "Stored conversation situation is not a valid object."
        )

    revision = _normalise_conversation_situation_revision(
        raw.get("revision"),
        field_name="stored conversation situation revision",
    )
    text = raw.get("text")
    if text is not None:
        if not isinstance(text, str):
            raise ChatHistoryServiceError(
                "Stored conversation situation text is not valid."
            )
        if len(text) > CONVERSATION_SITUATION_TEXT_MAX_CHARS:
            raise ChatHistoryServiceError(
                "Stored conversation situation text exceeds the configured limit."
            )

    updated_at = _coerce_datetime(raw.get("updated_at"))
    descriptor = {
        "text": text,
        "revision": revision,
        "source": _normalise_conversation_situation_metadata(
            raw.get("source"),
            field_name="stored conversation situation source",
            max_chars=_CONVERSATION_SITUATION_SOURCE_MAX_CHARS,
            required=False,
        ),
        "updated_by": _normalise_conversation_situation_metadata(
            raw.get("updated_by"),
            field_name="stored conversation situation updater",
            max_chars=_CONVERSATION_SITUATION_UPDATER_MAX_CHARS,
            required=False,
        ),
        "updated_at": updated_at.isoformat() if updated_at else None,
    }
    source_request_id = _normalise_conversation_situation_metadata(
        raw.get("source_request_id"),
        field_name="stored conversation situation source request",
        max_chars=_CONVERSATION_SITUATION_REQUEST_ID_MAX_CHARS,
        required=False,
    )
    if source_request_id is not None:
        descriptor["source_request_id"] = source_request_id
    return descriptor


def get_chat_history_session_state(
    *,
    user_id: str,
    session_id: str,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    history_tail_limit: Optional[int] = None,
    include_debug: bool = True,
    include_history: bool = True,
) -> Optional[Dict[str, Any]]:
    """Return bounded state carried by one canonical chat-history session.

    This is deliberately separate from :func:`get_chat_history`, whose
    established list return type remains the message-history contract.
    Selecting a shared session owner is a route/authority concern; this read
    only resolves the explicitly supplied owner, session, and namespace.
    """
    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id:
        raise ChatHistoryServiceError("session_id is required.")

    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    _guard_chat_history_read("get_chat_history_session_state")

    try:
        doc = None
        history_length: Optional[int] = None
        for query in _build_chat_history_session_read_queries(
            user_id=user_id,
            session_id=session_id,
            namespace=namespace,
            include_legacy=include_legacy,
        ):
            if (
                include_history
                and isinstance(history_tail_limit, int)
                and not isinstance(history_tail_limit, bool)
                and history_tail_limit > 0
                and hasattr(chat_history_coll, "aggregate")
                and callable(getattr(chat_history_coll, "aggregate"))
            ):
                pipeline = [
                    {"$match": query},
                    {
                        "$project": {
                            "_id": 0,
                            "session_id": 1,
                            "history": _history_tail_projection_expr(
                                history_tail_limit=history_tail_limit,
                                include_debug=include_debug,
                            ),
                            "history_length": {
                                "$size": _history_array_expr()
                            },
                            "conversation_situation": 1,
                            "conversation_observations": 1,
                            "conversation_observation_total": 1,
                            "focal_concept_ids": 1,
                            "focal_concept_ids_source": 1,
                            "focal_concept_ids_updated_at": 1,
                            "mode": 1,
                            "origin_kind": 1,
                            "concept_q_and_a.schema_version": 1,
                            "concept_q_and_a.concept": 1,
                            "concept_q_and_a.lifecycle": 1,
                            "concept_q_and_a.initial_question": 1,
                        }
                    },
                ]
                try:
                    doc = next(
                        _read_aggregate(
                            chat_history_coll,
                            pipeline,
                            operation=(
                                "get_chat_history_session_state.aggregate_tail"
                            ),
                        ),
                        None,
                    )
                except PyMongoError:
                    raise
                except Exception:
                    doc = None
            if doc is None:
                projection = {
                    "_id": 0,
                    "session_id": 1,
                    "conversation_situation": 1,
                    "conversation_observations": 1,
                    "conversation_observation_total": 1,
                    "focal_concept_ids": 1,
                    "focal_concept_ids_source": 1,
                    "focal_concept_ids_updated_at": 1,
                    "mode": 1,
                    "origin_kind": 1,
                    "concept_q_and_a.schema_version": 1,
                    "concept_q_and_a.concept": 1,
                    "concept_q_and_a.lifecycle": 1,
                    "concept_q_and_a.initial_question": 1,
                }
                if include_history:
                    projection["history"] = 1
                doc = _read_find_one(
                    chat_history_coll,
                    query,
                    projection,
                    operation="get_chat_history_session_state.find_session",
                )
            if doc is not None:
                raw_history_length = doc.get("history_length")
                if isinstance(raw_history_length, int):
                    history_length = raw_history_length
                break
        _record_chat_history_read_success()
    except PyMongoError as e:
        _record_chat_history_read_failure("get_chat_history_session_state", e)
        logger.exception("Error retrieving chat history session state: %s", e)
        raise ChatHistoryServiceError(
            f"Could not retrieve chat history session state: {e}"
        ) from e

    if not isinstance(doc, dict):
        return None
    history = (
        _normalise_chat_history_entries(doc.get("history", []))
        if include_history
        else []
    )
    state = {
        "session_id": session_id,
        "history": history,
        **_conversation_state_from_doc(doc),
        **project_chat_session_mode_state(doc),
    }
    focus = _focus_projection_from_doc(doc)
    if focus["focal_concept_ids"]:
        state.update(focus)
    if (
        include_history
        and isinstance(history_tail_limit, int)
        and not isinstance(history_tail_limit, bool)
        and history_tail_limit > 0
    ):
        if isinstance(history_length, int) and history_length >= 0:
            state["history_offset"] = max(0, history_length - len(history))
            state["history_truncated"] = history_length > len(history)
        else:
            state["history_offset"] = 0
            state["history_truncated"] = len(history) >= history_tail_limit
    return state


def _compare_and_set_chat_history_conversation_situation(
    *,
    user_id: str,
    session_id: str,
    text: Optional[str],
    expected_revision: int,
    source: str,
    updated_by: str,
    namespace: Optional[str],
    include_legacy: bool,
    source_request_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id:
        raise ChatHistoryServiceError("session_id is required.")

    revision = _normalise_conversation_situation_revision(expected_revision)
    source_value = _normalise_conversation_situation_metadata(
        source,
        field_name="source",
        max_chars=_CONVERSATION_SITUATION_SOURCE_MAX_CHARS,
        required=True,
    )
    updater_value = _normalise_conversation_situation_metadata(
        updated_by,
        field_name="updated_by",
        max_chars=_CONVERSATION_SITUATION_UPDATER_MAX_CHARS,
        required=True,
    )
    source_request_value = _normalise_conversation_situation_metadata(
        source_request_id,
        field_name="source_request_id",
        max_chars=_CONVERSATION_SITUATION_REQUEST_ID_MAX_CHARS,
        required=False,
    )

    if text is not None:
        if not isinstance(text, str) or not text.strip():
            raise ChatHistoryServiceError(
                "text must be a non-empty string; use the clear helper to reset it."
            )
        if len(text) > CONVERSATION_SITUATION_TEXT_MAX_CHARS:
            raise ChatHistoryServiceError(
                "text must be "
                f"{CONVERSATION_SITUATION_TEXT_MAX_CHARS} characters or fewer."
            )

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    next_revision = revision + 1
    updated_at = datetime.now(timezone.utc)
    stored_situation: Dict[str, Any] = {
        "revision": next_revision,
        "source": source_value,
        "updated_by": updater_value,
        "updated_at": updated_at,
    }
    if text is not None:
        stored_situation["text"] = text
    if source_request_value is not None:
        stored_situation["source_request_id"] = source_request_value

    session_query = build_chat_history_query(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_legacy=include_legacy,
    )
    revision_query: Dict[str, Any]
    if revision == 0:
        revision_query = {
            "$or": [
                {"conversation_situation": {"$exists": False}},
                {"conversation_situation": None},
            ]
        }
    else:
        revision_query = {"conversation_situation.revision": revision}
    cas_query = {"$and": [session_query, revision_query]}

    try:
        result = chat_history_coll.update_one(
            cas_query,
            {"$set": {"conversation_situation": stored_situation}},
        )
    except PyMongoError as e:
        logger.error(
            "Error updating chat history conversation situation: %s",
            e,
            exc_info=True,
        )
        raise ChatHistoryServiceError(
            f"Could not update chat history conversation situation: {e}"
        ) from e

    matched = bool(getattr(result, "matched_count", 0) > 0)
    updated = bool(getattr(result, "modified_count", 0) > 0)
    if matched and not updated:
        raise ChatHistoryServiceError(
            "Conversation situation update matched but was not persisted."
        )

    if updated:
        returned_situation = {
            "text": text,
            "revision": next_revision,
            "source": source_value,
            "updated_by": updater_value,
            "updated_at": updated_at.isoformat(),
        }
        if source_request_value is not None:
            returned_situation["source_request_id"] = source_request_value
        return {
            "updated": True,
            "matched": True,
            "conflict": False,
            "expected_revision": revision,
            "current_revision": next_revision,
            "session_id": session_id,
            "conversation_situation": returned_situation,
        }

    current_state = get_chat_history_session_state(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_legacy=include_legacy,
        include_history=False,
    )
    current_situation = (
        current_state.get("conversation_situation")
        if isinstance(current_state, dict)
        else None
    )
    current_revision = (
        current_situation.get("revision")
        if isinstance(current_situation, dict)
        else None
    )
    session_exists = current_state is not None
    return {
        "updated": False,
        "matched": session_exists,
        "conflict": session_exists,
        "expected_revision": revision,
        "current_revision": current_revision,
        "session_id": session_id,
        "conversation_situation": current_situation,
    }


def set_chat_history_conversation_situation(
    *,
    user_id: str,
    session_id: str,
    text: str,
    expected_revision: int,
    source: str,
    updated_by: str,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    source_request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare-and-set the inspectable text carrying the conversation situation."""
    return _compare_and_set_chat_history_conversation_situation(
        user_id=user_id,
        session_id=session_id,
        text=text,
        expected_revision=expected_revision,
        source=source,
        updated_by=updated_by,
        namespace=namespace,
        include_legacy=include_legacy,
        source_request_id=source_request_id,
    )


def clear_chat_history_conversation_situation(
    *,
    user_id: str,
    session_id: str,
    expected_revision: int,
    source: str,
    updated_by: str,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    source_request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Clear situation text while advancing its revision to prevent stale writes."""
    return _compare_and_set_chat_history_conversation_situation(
        user_id=user_id,
        session_id=session_id,
        text=None,
        expected_revision=expected_revision,
        source=source,
        updated_by=updated_by,
        namespace=namespace,
        include_legacy=include_legacy,
        source_request_id=source_request_id,
    )


_CONVERSATION_OBSERVATION_STRING_FIELDS = frozenset(
    {
        "schema_version",
        "observation_id",
        "kind",
        "observed_at_utc",
        "request_id",
        "effect_id",
        "call_id",
        "capability_name",
        "execution_id",
        "workflow_id",
        "instance_id",
        "terminal_status",
        "effect_status",
        "domain_postcondition_status",
        "outcome_finality",
        "final_state",
        "completed_at",
        "execution_trace_id",
    }
)


def _normalise_conversation_observation(
    value: Any,
    *,
    strict: bool,
) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        if strict:
            raise ChatHistoryServiceError("observation must be an object.")
        return None

    try:
        observation_id = _normalise_conversation_situation_metadata(
            value.get("observation_id"),
            field_name="observation.observation_id",
            max_chars=_CONVERSATION_OBSERVATION_ID_MAX_CHARS,
            required=strict,
        )
        kind = _normalise_conversation_situation_metadata(
            value.get("kind"),
            field_name="observation.kind",
            max_chars=_CONVERSATION_OBSERVATION_KIND_MAX_CHARS,
            required=strict,
        )
    except ChatHistoryServiceError:
        if strict:
            raise
        return None
    if not observation_id or not kind:
        return None

    normalised: Dict[str, Any] = {
        "schema_version": "conversation_observation.v1",
        "observation_id": observation_id,
        "kind": kind,
    }
    for field_name in _CONVERSATION_OBSERVATION_STRING_FIELDS:
        if field_name in {"schema_version", "observation_id", "kind"}:
            continue
        raw = value.get(field_name)
        if raw is None:
            continue
        if not isinstance(raw, str):
            if strict:
                raise ChatHistoryServiceError(
                    f"observation.{field_name} must be a string."
                )
            continue
        cleaned = raw.strip()
        if not cleaned:
            continue
        if len(cleaned) > _CONVERSATION_OBSERVATION_VALUE_MAX_CHARS:
            if strict:
                raise ChatHistoryServiceError(
                    f"observation.{field_name} must be "
                    f"{_CONVERSATION_OBSERVATION_VALUE_MAX_CHARS} characters "
                    "or fewer."
                )
            cleaned = cleaned[:_CONVERSATION_OBSERVATION_VALUE_MAX_CHARS]
        normalised[field_name] = cleaned

    for boolean_field in ("changed", "failure_detail_available"):
        boolean_value = value.get(boolean_field)
        if isinstance(boolean_value, bool):
            normalised[boolean_field] = boolean_value
        elif boolean_value is not None and strict:
            raise ChatHistoryServiceError(
                f"observation.{boolean_field} must be a boolean."
            )
    return normalised


def _conversation_observations_from_doc(
    doc: Dict[str, Any],
) -> List[Dict[str, Any]]:
    raw_observations = doc.get("conversation_observations")
    if raw_observations is None:
        return []
    if not isinstance(raw_observations, list):
        logger.warning(
            "Ignoring malformed conversation observations for session_id=%s",
            doc.get("session_id"),
        )
        return []

    observations: List[Dict[str, Any]] = []
    for raw in raw_observations[-CONVERSATION_OBSERVATION_MAX_ITEMS:]:
        normalised = _normalise_conversation_observation(raw, strict=False)
        if normalised is not None:
            observations.append(normalised)
    return observations


def _conversation_state_from_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Return bounded conversation state without making an optional carrier brittle."""

    try:
        conversation_situation = _conversation_situation_from_doc(doc)
    except ChatHistoryServiceError as exc:
        logger.warning(
            "Ignoring malformed conversation situation for session_id=%s: %s",
            doc.get("session_id"),
            exc,
        )
        conversation_situation = None

    observations = _conversation_observations_from_doc(doc)
    raw_observations = doc.get("conversation_observations")
    raw_retained_count = (
        len(raw_observations) if isinstance(raw_observations, list) else 0
    )
    raw_observation_total = doc.get("conversation_observation_total")
    observation_total = (
        raw_observation_total
        if isinstance(raw_observation_total, int)
        and not isinstance(raw_observation_total, bool)
        and raw_observation_total >= 0
        else raw_retained_count
    )
    observation_total = max(
        observation_total,
        raw_retained_count,
        len(observations),
    )
    return {
        "conversation_situation": conversation_situation,
        "conversation_observations": observations,
        "conversation_observation_state": {
            "schema_version": "conversation_observation_state.v1",
            "retained_count": len(observations),
            "total_count": observation_total,
            "omitted_count": max(0, observation_total - len(observations)),
            "retention_limit": CONVERSATION_OBSERVATION_MAX_ITEMS,
        },
    }


def _conversation_read_metadata_from_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Return bounded identity and dates from the same actor-scoped history read."""

    created_at = _infer_created_timestamp(doc)
    last_message_at = _infer_last_message_timestamp(
        {
            "history": doc.get("history"),
            "created_at": created_at,
        }
    )
    return {
        "session_name": _normalise_session_name(doc.get("session_name")),
        "last_message_at": (
            last_message_at.isoformat() if last_message_at is not None else None
        ),
        "created_at": created_at.isoformat() if created_at is not None else None,
    }


def append_chat_history_conversation_observation(
    *,
    user_id: str,
    session_id: str,
    observation: Dict[str, Any],
    namespace: Optional[str] = None,
    include_legacy: bool = True,
) -> Dict[str, Any]:
    """Append one bounded exact observation, idempotently while it is retained.

    These observations supplement the model-authored situation text with
    mechanically exact events from other carriers, such as a durable workflow
    reaching terminal state after its originating turn ended. They are context,
    not authority, and this projection never executes or retries the effect.
    Producers that may replay after this bounded ring evicts an item must keep
    their own durable projection watermark.
    """

    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id:
        raise ChatHistoryServiceError("session_id is required.")
    normalised = _normalise_conversation_observation(observation, strict=True)
    if normalised is None:
        raise ChatHistoryServiceError("observation is required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    session_query = build_chat_history_query(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_legacy=include_legacy,
    )
    observation_id = normalised["observation_id"]
    append_query = {
        "$and": [
            session_query,
            {
                "conversation_observations.observation_id": {
                    "$ne": observation_id
                }
            },
        ]
    }
    try:
        existing_observations_expression = {
            "$cond": [
                {"$isArray": "$conversation_observations"},
                "$conversation_observations",
                [],
            ]
        }
        stored_total_expression = {
            "$cond": [
                {"$isNumber": "$conversation_observation_total"},
                {"$floor": "$conversation_observation_total"},
                0,
            ]
        }
        result = chat_history_coll.update_one(
            append_query,
            [
                {
                    "$set": {
                        "conversation_observations": {
                            "$slice": [
                                {
                                    "$concatArrays": [
                                        existing_observations_expression,
                                        [normalised],
                                    ]
                                },
                                -CONVERSATION_OBSERVATION_MAX_ITEMS,
                            ]
                        },
                        "conversation_observation_total": {
                            "$add": [
                                {
                                    "$max": [
                                        {
                                            "$size": (
                                                existing_observations_expression
                                            )
                                        },
                                        stored_total_expression,
                                    ]
                                },
                                1,
                            ]
                        },
                    }
                }
            ],
        )
    except PyMongoError as e:
        logger.error(
            "Error appending chat history conversation observation: %s",
            e,
            exc_info=True,
        )
        raise ChatHistoryServiceError(
            f"Could not append chat history conversation observation: {e}"
        ) from e

    updated = bool(getattr(result, "modified_count", 0) > 0)
    if updated:
        return {
            "updated": True,
            "matched": True,
            "duplicate": False,
            "session_id": session_id,
            "observation_id": observation_id,
        }

    current_state = get_chat_history_session_state(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_legacy=include_legacy,
        include_history=False,
    )
    raw_observations = (
        current_state.get("conversation_observations")
        if isinstance(current_state, dict)
        else []
    )
    observations = raw_observations if isinstance(raw_observations, list) else []
    duplicate = any(
        isinstance(item, dict) and item.get("observation_id") == observation_id
        for item in observations
    )
    return {
        "updated": False,
        "matched": current_state is not None,
        "duplicate": duplicate,
        "reason": "duplicate" if duplicate else "session_not_found",
        "session_id": session_id,
        "observation_id": observation_id,
    }


def clear_chat_history_conversation_observations(
    *,
    user_id: str,
    session_id: str,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
) -> Dict[str, Any]:
    """Clear prior exact observations when the conversation is explicitly reset."""

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
        result = chat_history_coll.update_one(
            query,
            {
                "$unset": {
                    "conversation_observations": "",
                    "conversation_observation_total": "",
                }
            },
        )
    except PyMongoError as e:
        logger.error(
            "Error clearing chat history conversation observations: %s",
            e,
            exc_info=True,
        )
        raise ChatHistoryServiceError(
            f"Could not clear chat history conversation observations: {e}"
        ) from e
    return {
        "updated": bool(getattr(result, "modified_count", 0) > 0),
        "matched": bool(getattr(result, "matched_count", 0) > 0),
        "session_id": session_id,
    }


def reset_chat_history_conversation_state(
    *,
    user_id: str,
    session_id: str,
    updated_by: str,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
) -> Dict[str, Any]:
    """Atomically start a new transcript segment and reset its carried state.

    The atomic update gives reset a single ordering point. A turn that loaded
    the pre-reset situation cannot later overwrite the cleared revision, while
    a turn that starts after reset may revise the new situation normally.
    """

    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id:
        raise ChatHistoryServiceError("session_id is required.")
    updater_value = _normalise_conversation_situation_metadata(
        updated_by,
        field_name="updated_by",
        max_chars=_CONVERSATION_SITUATION_UPDATER_MAX_CHARS,
        required=True,
    )
    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    query = build_chat_history_query(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_legacy=include_legacy,
    )
    updated_at = datetime.now(timezone.utc)
    reset_marker = {
        "role": "system",
        "content": "__RESET__",
        "timestamp": updated_at,
    }
    update_pipeline = [
        {
            "$set": {
                "history": {
                    "$concatArrays": [
                        {"$ifNull": ["$history", []]},
                        [reset_marker],
                    ]
                },
                "conversation_situation": {
                    "revision": {
                        "$add": [
                            {
                                "$convert": {
                                    "input": "$conversation_situation.revision",
                                    "to": "int",
                                    "onError": 0,
                                    "onNull": 0,
                                }
                            },
                            1,
                        ]
                    },
                    "source": "conversation_reset",
                    "updated_by": updater_value,
                    "updated_at": updated_at,
                },
                "updated_at": updated_at,
            }
        },
        {"$unset": "conversation_observations"},
        {"$unset": "conversation_observation_total"},
    ]
    try:
        result = chat_history_coll.update_one(query, update_pipeline)
    except PyMongoError as e:
        logger.error(
            "Error atomically resetting chat history conversation state: %s",
            e,
            exc_info=True,
        )
        raise ChatHistoryServiceError(
            f"Could not reset chat history conversation state: {e}"
        ) from e
    return {
        "updated": bool(getattr(result, "modified_count", 0) > 0),
        "matched": bool(getattr(result, "matched_count", 0) > 0),
        "session_id": session_id,
    }


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


def _conversation_locator_state_from_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Return compact carrier availability/freshness without exposing its content."""

    raw_situation_state = doc.get("conversation_situation_state")
    raw_situation = doc.get("conversation_situation")
    if isinstance(raw_situation_state, dict):
        situation_available = raw_situation_state.get("available") is True
        raw_revision = raw_situation_state.get("revision")
        raw_updated_at = raw_situation_state.get("updated_at")
    else:
        situation_available = bool(
            isinstance(raw_situation, dict)
            and isinstance(raw_situation.get("text"), str)
            and raw_situation.get("text", "").strip()
        )
        raw_revision = (
            raw_situation.get("revision") if isinstance(raw_situation, dict) else None
        )
        raw_updated_at = (
            raw_situation.get("updated_at") if isinstance(raw_situation, dict) else None
        )
    situation_revision = (
        raw_revision
        if isinstance(raw_revision, int)
        and not isinstance(raw_revision, bool)
        and raw_revision >= 0
        else None
    )
    situation_updated_at = _coerce_datetime(raw_updated_at)

    raw_observation_state = doc.get("conversation_observation_state")
    raw_observations = doc.get("conversation_observations")
    fallback_retained_count = (
        len(raw_observations) if isinstance(raw_observations, list) else 0
    )
    retained_candidate = (
        raw_observation_state.get("retained_count")
        if isinstance(raw_observation_state, dict)
        else fallback_retained_count
    )
    retained_count = (
        retained_candidate
        if isinstance(retained_candidate, int)
        and not isinstance(retained_candidate, bool)
        and retained_candidate >= 0
        else fallback_retained_count
    )
    total_candidate = (
        raw_observation_state.get("total_count")
        if isinstance(raw_observation_state, dict)
        else doc.get("conversation_observation_total")
    )
    total_count = (
        total_candidate
        if isinstance(total_candidate, int)
        and not isinstance(total_candidate, bool)
        and total_candidate >= 0
        else retained_count
    )
    total_count = max(total_count, retained_count)
    return {
        "conversation_situation_state": {
            "available": situation_available,
            "revision": situation_revision,
            "updated_at": (
                situation_updated_at.isoformat() if situation_updated_at else None
            ),
        },
        "conversation_observation_state": {
            "schema_version": "conversation_observation_state.v1",
            "retained_count": retained_count,
            "total_count": total_count,
            "omitted_count": max(0, total_count - retained_count),
            "retention_limit": CONVERSATION_OBSERVATION_MAX_ITEMS,
        },
    }


def get_chat_history_telemetry_locator_projection(
    user_id: str,
    session_id: str,
    *,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
) -> Optional[Dict[str, Any]]:
    """Return only session and per-entry fields needed to build telemetry locators."""

    if not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if not session_id:
        raise ChatHistoryServiceError("session_id is required.")

    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    _guard_chat_history_read("get_chat_history_telemetry_locator_projection")

    compact_history_projection = {
        "$map": {
            "input": {"$ifNull": ["$history", []]},
            "as": "entry",
            "in": {
                "role": "$$entry.role",
                "sender": "$$entry.sender",
                "turn_id": "$$entry.turn_id",
                "id": "$$entry.id",
                "message_id": "$$entry.message_id",
                "timestamp": "$$entry.timestamp",
                "created_at": "$$entry.created_at",
                "llm_debug_data": {
                    "$cond": [
                        {"$eq": [{"$type": "$$entry.llm_debug_data"}, "object"]},
                        {
                            "request_id": "$$entry.llm_debug_data.request_id",
                            "turn_id": "$$entry.llm_debug_data.turn_id",
                            "timestamp_utc": "$$entry.llm_debug_data.timestamp_utc",
                        },
                        None,
                    ]
                },
            },
        }
    }
    metadata_projection = {
        "_id": 0,
        "user_id": 1,
        "session_id": 1,
        "session_name": 1,
        "namespace": 1,
        "organisation_concept_id": 1,
        "created_at": 1,
        "updated_at": 1,
        "history": compact_history_projection,
        "conversation_situation_state": {
            "available": {
                "$cond": [
                    {
                        "$eq": [
                            {"$type": "$conversation_situation.text"},
                            "string",
                        ]
                    },
                    {
                        "$gt": [
                            {"$strLenCP": "$conversation_situation.text"},
                            0,
                        ]
                    },
                    False,
                ]
            },
            "revision": "$conversation_situation.revision",
            "updated_at": "$conversation_situation.updated_at",
        },
        "conversation_observation_state": {
            "retained_count": {
                "$size": {
                    "$cond": [
                        {"$isArray": "$conversation_observations"},
                        "$conversation_observations",
                        [],
                    ]
                }
            },
            "total_count": "$conversation_observation_total",
        },
    }

    try:
        doc = None
        for query in _build_chat_history_session_read_queries(
            user_id=user_id,
            session_id=session_id,
            namespace=namespace,
            include_legacy=include_legacy,
        ):
            if hasattr(chat_history_coll, "aggregate") and callable(
                getattr(chat_history_coll, "aggregate")
            ):
                doc = next(
                    _read_aggregate(
                        chat_history_coll,
                        [
                            {"$match": query},
                            {"$limit": 1},
                            {"$project": metadata_projection},
                        ],
                        operation=(
                            "get_chat_history_telemetry_locator_projection.aggregate"
                        ),
                    ),
                    None,
                )
            else:
                # Test doubles and older collection adapters may not expose aggregate.
                fallback_projection = {
                    key: value
                    for key, value in metadata_projection.items()
                    if key
                    not in {
                        "history",
                        "conversation_situation_state",
                        "conversation_observation_state",
                    }
                } | {
                    "history": 1,
                    "conversation_situation": 1,
                    "conversation_observations": 1,
                    "conversation_observation_total": 1,
                }
                doc = _read_find_one(
                    chat_history_coll,
                    query,
                    fallback_projection,
                    operation=(
                        "get_chat_history_telemetry_locator_projection.find_fallback"
                    ),
                )
            if doc is not None:
                break
        _record_chat_history_read_success()
        if not isinstance(doc, dict):
            return None
        payload = dict(doc)
        payload.update(_conversation_locator_state_from_doc(payload))
        payload.pop("conversation_situation", None)
        payload.pop("conversation_observations", None)
        payload.pop("conversation_observation_total", None)
        return payload
    except PyMongoError as exc:
        _record_chat_history_read_failure(
            "get_chat_history_telemetry_locator_projection",
            exc,
        )
        logger.error(
            "Error retrieving compact telemetry locator projection: %s",
            exc,
            exc_info=True,
        )
        raise ChatHistoryServiceError(
            f"Could not retrieve telemetry locator projection: {exc}"
        ) from exc


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
    include_conversation_state: bool = False,
) -> List[List[Dict[str, Any]]] | tuple[List[List[Dict[str, Any]]], Dict[str, Any]]:
    """
    Return chat history split into segments separated by reset markers.
    Retrieves history from the requested session only.

    When return_meta is True, returns ``(segments, metadata)``. Callers may opt
    into bounded conversation state in that metadata without another DB read.
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
        projection = (
            {
                "history": 1,
                "session_name": 1,
                "created_at": 1,
                "updated_at": 1,
                "conversation_situation": 1,
                "conversation_observations": 1,
                "conversation_observation_total": 1,
            }
            if include_conversation_state
            else None
        )
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
                    tail_projection = {
                        "history": _history_tail_projection_expr(
                            history_tail_limit=history_tail_limit,
                            include_debug=include_debug,
                        ),
                        "history_length": {"$size": _history_array_expr()},
                    }
                    if include_conversation_state:
                        tail_projection.update(
                            {
                                "session_name": 1,
                                "created_at": 1,
                                "updated_at": 1,
                                "conversation_situation": 1,
                                "conversation_observations": 1,
                                "conversation_observation_total": 1,
                            }
                        )
                    pipeline = [
                        {"$match": query},
                        {"$project": tail_projection},
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
            empty_meta = {"history_truncated": False}
            if include_conversation_state:
                empty_meta.update(_conversation_state_from_doc({}))
                empty_meta.update(_conversation_read_metadata_from_doc({}))
            return ([], empty_meta) if return_meta else []

        history = _normalise_chat_history_entries(
            doc.get("history") or [],
            hydrate_blob_refs=hydrate_blob_refs,
        )
        if not isinstance(history, list) or not history:
            _record_chat_history_read_success()
            empty_history_meta = {"history_truncated": False}
            if include_conversation_state:
                empty_history_meta.update(_conversation_state_from_doc(doc))
                empty_history_meta.update(_conversation_read_metadata_from_doc(doc))
            return ([], empty_history_meta) if return_meta else []
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
            result_meta = {"history_truncated": history_truncated}
            if include_conversation_state:
                result_meta.update(_conversation_state_from_doc(doc))
                result_meta.update(_conversation_read_metadata_from_doc(doc))
            return result, result_meta
        return result
    except PyMongoError as e:
        _record_chat_history_read_failure("get_chat_history_segments", e)
        logger.error(f"Error retrieving segmented chat history: {e}", exc_info=True)
        raise ChatHistoryServiceError(
            f"Could not retrieve segmented chat history: {e}"
        ) from e


def get_chat_history_transcript_page(
    *,
    user_id: str,
    session_id: str,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    offset: int = 0,
    page_size: int = 50,
) -> Dict[str, Any]:
    """Return one exact source-level transcript page without debug payloads."""

    if not isinstance(user_id, str) or not user_id.strip():
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ChatHistoryServiceError("session_id is required.")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ChatHistoryServiceError("offset must be a non-negative integer.")
    if not isinstance(page_size, int) or isinstance(page_size, bool):
        raise ChatHistoryServiceError("page_size must be an integer.")
    safe_page_size = max(1, min(page_size, 100))
    collection = get_chat_history_collection_service(read_only=True)
    if collection is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")
    _guard_chat_history_read("get_chat_history_transcript_page")

    document = None
    try:
        for query in _build_chat_history_session_read_queries(
            user_id=user_id.strip(),
            session_id=session_id.strip(),
            namespace=namespace,
            include_legacy=include_legacy,
        ):
            pipeline = [
                {"$match": query},
                {
                    "$project": {
                        "_id": 0,
                        "session_id": 1,
                        "session_name": 1,
                        "created_at": 1,
                        "updated_at": 1,
                        "history_length": {"$size": _history_array_expr()},
                        "history_page": {
                            "$slice": [
                                _history_array_expr(),
                                offset,
                                safe_page_size,
                            ]
                        },
                    }
                },
            ]
            document = next(
                _read_aggregate(
                    collection,
                    pipeline,
                    operation="get_chat_history_transcript_page.aggregate",
                ),
                None,
            )
            if isinstance(document, Mapping):
                break
        _record_chat_history_read_success()
    except PyMongoError as exc:
        _record_chat_history_read_failure("get_chat_history_transcript_page", exc)
        raise ChatHistoryServiceError(
            f"Could not page conversation transcript: {exc}"
        ) from exc

    if not isinstance(document, Mapping):
        return {
            "found": False,
            "messages": [],
            "message_count": 0,
            "offset": offset,
            "next_offset": None,
            "has_more": False,
            "coverage_complete": False,
        }
    raw_messages = document.get("history_page")
    messages: List[Dict[str, Any]] = []
    if isinstance(raw_messages, list):
        for relative_index, entry in enumerate(raw_messages):
            if not isinstance(entry, Mapping):
                continue
            cleaned = dict(entry)
            cleaned.pop("llm_debug_data", None)
            cleaned["source_locator"] = {
                "session_id": session_id.strip(),
                "history_index": offset + relative_index,
                "turn_id": cleaned.get("turn_id"),
                "message_id": cleaned.get("message_id"),
            }
            messages.append(cleaned)
    history_length = document.get("history_length")
    if not isinstance(history_length, int) or history_length < 0:
        history_length = offset + len(messages)
    next_offset = offset + len(raw_messages or [])
    has_more = next_offset < history_length
    return {
        "found": True,
        "session_id": session_id.strip(),
        "session_name": _normalise_session_name(document.get("session_name")),
        "created_at": _isoformat_datetime(document.get("created_at")),
        "updated_at": _isoformat_datetime(document.get("updated_at")),
        "messages": messages,
        "message_count": history_length,
        "page_count": len(messages),
        "offset": offset,
        "next_offset": next_offset if has_more else None,
        "has_more": has_more,
        "coverage_complete": not has_more,
    }


def get_chat_history_title_evidence(
    *,
    user_id: str,
    session_id: str,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    excerpt_chars: int = 2_000,
) -> Optional[Dict[str, Any]]:
    """Return bounded first/latest user-message evidence for title judgement."""

    if not isinstance(user_id, str) or not user_id.strip():
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ChatHistoryServiceError("session_id is required.")
    safe_excerpt_chars = max(200, min(int(excerpt_chars), 4_000))
    collection = get_chat_history_collection_service(read_only=True)
    if collection is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")
    _guard_chat_history_read("get_chat_history_title_evidence")

    document = None
    try:
        for query in _build_chat_history_session_read_queries(
            user_id=user_id.strip(),
            session_id=session_id.strip(),
            namespace=namespace,
            include_legacy=include_legacy,
        ):
            pipeline = [
                {"$match": query},
                {
                    "$project": {
                        "_id": 0,
                        "session_id": 1,
                        "session_name": 1,
                        "created_at": 1,
                        "updated_at": 1,
                        "history_length": {"$size": _history_array_expr()},
                        "user_messages": {
                            "$filter": {
                                "input": _history_array_expr(),
                                "as": "message",
                                "cond": {"$eq": ["$$message.role", "user"]},
                            }
                        },
                    }
                },
                {
                    "$project": {
                        "session_id": 1,
                        "session_name": 1,
                        "created_at": 1,
                        "updated_at": 1,
                        "history_length": 1,
                        "user_message_count": {"$size": "$user_messages"},
                        "first_user_message": {
                            "$arrayElemAt": ["$user_messages", 0]
                        },
                        "latest_user_message": {
                            "$arrayElemAt": ["$user_messages", -1]
                        },
                    }
                },
            ]
            document = next(
                _read_aggregate(
                    collection,
                    pipeline,
                    operation="get_chat_history_title_evidence.aggregate",
                ),
                None,
            )
            if isinstance(document, Mapping):
                break
        _record_chat_history_read_success()
    except PyMongoError as exc:
        _record_chat_history_read_failure("get_chat_history_title_evidence", exc)
        raise ChatHistoryServiceError(
            f"Could not read conversation title evidence: {exc}"
        ) from exc
    if not isinstance(document, Mapping):
        return None

    def _project_message(value: Any, *, position: str) -> Optional[Dict[str, Any]]:
        if not isinstance(value, Mapping):
            return None
        content = value.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        cleaned = content.strip()
        excerpt = cleaned[:safe_excerpt_chars]
        turn_id = value.get("turn_id")
        message_id = value.get("message_id")
        timestamp = _isoformat_datetime(value.get("timestamp"))
        return {
            "position": position,
            "role": "user",
            "content": excerpt,
            "content_truncated": len(cleaned) > len(excerpt),
            "content_char_count": len(cleaned),
            "content_sha256": hashlib.sha256(cleaned.encode("utf-8")).hexdigest(),
            "turn_id": turn_id,
            "message_id": message_id,
            "timestamp": timestamp,
            "source_locator": {
                "session_id": session_id.strip(),
                "turn_id": turn_id,
                "message_id": message_id,
                "timestamp": timestamp,
            },
        }

    first = _project_message(document.get("first_user_message"), position="first")
    latest = _project_message(document.get("latest_user_message"), position="latest")
    user_message_count = document.get("user_message_count")
    history_length = document.get("history_length")
    return {
        "session_id": session_id.strip(),
        "session_name": _normalise_session_name(document.get("session_name")),
        "created_at": _isoformat_datetime(document.get("created_at")),
        "updated_at": _isoformat_datetime(document.get("updated_at")),
        "message_count": history_length if isinstance(history_length, int) else None,
        "user_message_count": (
            user_message_count if isinstance(user_message_count, int) else 0
        ),
        "first_user_message": first,
        "latest_user_message": latest,
        "evidence_sufficient": first is not None and latest is not None,
    }


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

    actor = record.get("actor") if isinstance(record.get("actor"), dict) else {}
    actor_user_id = (
        _safe_str(actor.get("user_concept_id"))
        or _safe_str(record.get("user_id"))
        or _safe_str(user_id)
    )
    actor_namespace = (
        _safe_str(actor.get("namespace"))
        or _safe_str(record.get("namespace"))
        or _safe_str(namespace)
    )
    actor_org_id = (
        _safe_str(actor.get("organisation_concept_id"))
        or _safe_str(record.get("org_id"))
        or _safe_str(org_id)
    )
    projection_record = dict(record)
    projection_record["history_owner_user_id"] = user_id
    if _safe_str(namespace):
        projection_record["history_namespace"] = _safe_str(namespace)

    try:
        outcome = upsert_turn_execution_record_projection(
            record=projection_record,
            user_id=actor_user_id,
            session_id=session_id,
            namespace=actor_namespace,
            org_id=actor_org_id,
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
            record=projection_record,
            llm_debug_data=llm_debug_data,
            user_id=actor_user_id,
            session_id=session_id,
            namespace=actor_namespace,
            org_id=actor_org_id,
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

        # Keep diagnostics in the existing actor-scoped debug channel, not the
        # shared transcript. The request/enqueued envelope freezes its own client.
        from .speech_telemetry_service import request_client_context
        client_context = request_client_context()
        if client_context:
            llm_debug_data = llm_debug_data if isinstance(llm_debug_data, dict) else {}
            llm_debug_data["client_context"] = client_context

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
        search_segment = _conversation_search_segment(stored_message)
        search_text = search_segment.get("text") if search_segment is not None else None
        if search_text is not None:
            set_fields.update(
                {
                    "conversation_search_indexed_at": datetime.now(timezone.utc),
                }
            )
        # NOTE: namespace is intentionally NOT in $set - it should only be set
        # on document creation via $setOnInsert. Otherwise, when a shared
        # conversation participant adds a message, their namespace would
        # overwrite the owner's namespace and cause the conversation to
        # disappear from the owner's session list. (JVNAUTOSCI-1004)

        set_on_insert: Dict[str, Any] = {"created_at": datetime.now(timezone.utc)}
        if isinstance(ns, str) and ns.strip():
            set_on_insert["namespace"] = ns.strip()
        if isinstance(org_concept_id, str) and org_concept_id.strip():
            set_on_insert["organisation_concept_id"] = org_concept_id.strip()
        if search_text is not None:
            set_on_insert["conversation_search_index_version"] = (
                CONVERSATION_SEARCH_INDEX_VERSION
            )
        role_value = effective_session_context.get("role_in_org")
        if isinstance(role_value, str) and role_value.strip():
            set_on_insert["role_in_org"] = role_value.strip()

        # Update or insert the session document
        push_fields: Dict[str, Any] = {"history": stored_message}
        if search_text is not None:
            push_fields["conversation_search_text"] = search_text
            push_fields["conversation_search_segments"] = search_segment
        chat_history_coll.update_one(
            {"user_id": user_id, "session_id": session_id},
            {
                "$push": push_fields,
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


def append_message_to_history_once(
    *,
    user_id: str,
    session_id: str,
    message: Dict[str, Any],
    namespace: Optional[str] = None,
    expected_session_fields: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Atomically append one identified turn to an existing chat carrier.

    This is the exact-transcript primitive for retryable feature-specific
    conversations.  It never creates a session and it never treats client
    metadata as actor authority.  Reusing a ``turn_id`` returns the existing
    stored turn instead of appending another copy.
    """

    if not isinstance(user_id, str) or not user_id.strip():
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ChatHistoryServiceError("session_id is required.")
    if not isinstance(message, dict):
        raise ChatHistoryServiceError("message must be an object.")
    role = message.get("role")
    content = message.get("content")
    turn_id = message.get("turn_id")
    if role not in {"user", "assistant", "system", "tool"}:
        raise ChatHistoryServiceError("message.role is not supported.")
    if not isinstance(content, str):
        raise ChatHistoryServiceError("message.content must be a string.")
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise ChatHistoryServiceError("message.turn_id is required.")

    collection = get_chat_history_collection_service()
    if collection is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    base_query = build_chat_history_query(
        user_id=user_id.strip(),
        session_id=session_id.strip(),
        namespace=namespace,
        include_legacy=True,
    )
    if isinstance(expected_session_fields, Mapping):
        for field_name, expected_value in expected_session_fields.items():
            if not isinstance(field_name, str) or not field_name.strip():
                raise ChatHistoryServiceError("expected session field names are required.")
            base_query[field_name.strip()] = expected_value

    stored_message = dict(message)
    stored_message["turn_id"] = turn_id.strip()
    if role == "user" and not isinstance(stored_message.get("author_user_id"), str):
        stored_message["author_user_id"] = user_id.strip()
    timestamp = stored_message.get("timestamp")
    if not isinstance(timestamp, (datetime, str)):
        stored_message["timestamp"] = datetime.now(timezone.utc)

    set_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    push_fields: Dict[str, Any] = {"history": stored_message}
    search_segment = _conversation_search_segment(stored_message)
    if search_segment is not None:
        push_fields["conversation_search_text"] = search_segment["text"]
        push_fields["conversation_search_segments"] = search_segment
        set_fields["conversation_search_indexed_at"] = datetime.now(timezone.utc)

    append_query = dict(base_query)
    append_query["history.turn_id"] = {"$ne": turn_id.strip()}
    try:
        result = collection.update_one(
            append_query,
            {"$push": push_fields, "$set": set_fields},
        )
        doc = collection.find_one(base_query, {"history": 1})
    except PyMongoError as exc:
        raise ChatHistoryServiceError(
            f"Could not append identified chat history turn: {exc}"
        ) from exc

    if not isinstance(doc, dict):
        return {
            "matched": False,
            "appended": False,
            "duplicate": False,
            "turn_id": turn_id.strip(),
            "message": None,
            "history_index": None,
        }
    history = doc.get("history") if isinstance(doc.get("history"), list) else []
    existing_message = None
    history_index = None
    for index, entry in enumerate(history):
        if isinstance(entry, dict) and entry.get("turn_id") == turn_id.strip():
            existing_message = dict(entry)
            history_index = index
            break
    appended = bool(getattr(result, "modified_count", 0) > 0)
    return {
        "matched": True,
        "appended": appended,
        "duplicate": not appended and existing_message is not None,
        "turn_id": turn_id.strip(),
        "message": existing_message,
        "history_index": history_index,
    }


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


def classify_external_conversation_projection(
    *,
    user_id: str,
    session_id: str,
    namespace: Optional[str],
    source_identity_key: str,
    source_sha256: str,
    package_sha256: str,
    parser_version: str,
) -> str:
    """Classify an actor-scoped import without mutating its conversation."""

    if not user_id or not session_id or not source_identity_key:
        raise ChatHistoryServiceError(
            "user_id, session_id, and source_identity_key are required."
        )
    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")
    query = build_chat_history_query(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_legacy=True,
    )
    try:
        doc = _read_find_one(
            chat_history_coll,
            query,
            {EXTERNAL_CONVERSATION_IMPORT_FIELD: 1},
        )
    except PyMongoError as exc:
        raise ChatHistoryServiceError(
            f"Could not classify external conversation import: {exc}"
        ) from exc
    if doc is None:
        return "new"
    manifest = doc.get(EXTERNAL_CONVERSATION_IMPORT_FIELD)
    if not isinstance(manifest, Mapping):
        raise ChatHistoryServiceError(
            "The deterministic import session ID collides with a native conversation."
        )
    if manifest.get("source_identity_key") != source_identity_key:
        raise ChatHistoryServiceError(
            "The deterministic import session ID belongs to another external source."
        )
    unchanged = bool(
        manifest.get("read_only") is True
        and manifest.get("source_sha256") == source_sha256
        and manifest.get("package_sha256") == package_sha256
        and manifest.get("parser_version") == parser_version
    )
    return "unchanged" if unchanged else "updated"


def get_external_conversation_projection(
    *,
    user_id: str,
    session_id: str,
    namespace: Optional[str],
    include_history: bool = False,
) -> Optional[Dict[str, Any]]:
    """Read an imported projection only through the custodian's actor scope."""

    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")
    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")
    query = build_chat_history_query(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_legacy=True,
    )
    projection: Dict[str, Any] = {
        "_id": 0,
        "session_id": 1,
        "session_name": 1,
        "namespace": 1,
        "organisation_concept_id": 1,
        "role_in_org": 1,
        EXTERNAL_CONVERSATION_IMPORT_FIELD: 1,
        "conversation_lineage": 1,
    }
    if include_history:
        projection["history"] = 1
    try:
        doc = _read_find_one(chat_history_coll, query, projection)
    except PyMongoError as exc:
        raise ChatHistoryServiceError(
            f"Could not read external conversation projection: {exc}"
        ) from exc
    if not isinstance(doc, dict):
        return None
    manifest = doc.get(EXTERNAL_CONVERSATION_IMPORT_FIELD)
    if not isinstance(manifest, Mapping):
        return None
    return doc


def is_external_conversation_read_only(
    *,
    user_id: str,
    session_id: str,
    namespace: Optional[str],
) -> bool:
    doc = get_external_conversation_projection(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_history=False,
    )
    if not doc:
        return False
    manifest = doc.get(EXTERNAL_CONVERSATION_IMPORT_FIELD)
    return bool(isinstance(manifest, Mapping) and manifest.get("read_only") is True)


def upsert_external_conversation_projection(
    *,
    user_id: str,
    session_id: str,
    session_name: str,
    namespace: Optional[str],
    organisation_concept_id: Optional[str],
    role_in_org: Optional[str],
    history: List[Dict[str, Any]],
    manifest: Mapping[str, Any],
    _concurrency_attempt: int = 0,
) -> Dict[str, Any]:
    """Create or refresh a read-only imported transcript in actor scope."""

    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")
    if not isinstance(history, list) or not history:
        raise ChatHistoryServiceError("Imported history must contain messages.")
    if not isinstance(manifest, Mapping):
        raise ChatHistoryServiceError("An external import manifest is required.")
    stored_manifest = dict(manifest)
    if (
        stored_manifest.get("schema_version")
        != EXTERNAL_CONVERSATION_IMPORT_SCHEMA_VERSION
        or stored_manifest.get("read_only") is not True
        or stored_manifest.get("custodian_user_id") != user_id
        or not stored_manifest.get("source_identity_key")
    ):
        raise ChatHistoryServiceError("The external import manifest is invalid.")
    for entry in history:
        if not isinstance(entry, dict) or entry.get("role") not in {
            "user",
            "assistant",
        }:
            raise ChatHistoryServiceError(
                "Imported history may contain only user and assistant messages."
            )
        if entry.get("author_user_id") is not None:
            raise ChatHistoryServiceError(
                "External speakers cannot be attributed to the custodian user."
            )

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")
    now = datetime.now(timezone.utc)
    normalised_name = _normalise_session_name(session_name)
    exact_query: Dict[str, Any] = {"user_id": user_id, "session_id": session_id}
    if isinstance(namespace, str) and namespace.strip():
        exact_query["namespace"] = namespace.strip()
    existing = chat_history_coll.find_one(
        exact_query,
        {
            "session_name": 1,
            "history": 1,
            EXTERNAL_CONVERSATION_IMPORT_FIELD: 1,
        },
    )

    def _event_fingerprint(entry: Mapping[str, Any]) -> str:
        timestamp = entry.get("timestamp")
        if isinstance(timestamp, datetime):
            timestamp = timestamp.astimezone(timezone.utc).isoformat()
        payload = {
            key: entry.get(key)
            for key in (
                "role",
                "content",
                "external_event_id",
                "external_event_kind",
                "external_source_role",
                "external_actor",
                "external_parent_event_id",
                "external_branch_id",
                "external_model",
                "external_raw_locator",
            )
        }
        payload["source_timestamp_utc"] = (
            entry.get("external_source_timestamp_utc")
            if "external_source_timestamp_utc" in entry
            else timestamp
        )
        return hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    incoming_ids: list[str] = []
    incoming_id_set: set[str] = set()
    prepared_history: List[Dict[str, Any]] = []
    for index, entry in enumerate(history):
        event_id = entry.get("external_event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            legacy_identity = "|".join(
                str(entry.get(key) or "") for key in ("role", "content", "timestamp")
            )
            event_id = (
                "legacy:external:event:"
                + hashlib.sha256(
                    f"{index}|{legacy_identity}".encode("utf-8")
                ).hexdigest()[:24]
            )
        event_id = event_id.strip()
        if event_id in incoming_id_set:
            raise ChatHistoryServiceError(
                f"The imported source repeats external event {event_id}."
            )
        incoming_ids.append(event_id)
        incoming_id_set.add(event_id)
        prepared = dict(entry)
        prepared["external_event_id"] = event_id
        prepared["external_event_sha256"] = _event_fingerprint(prepared)
        prepared_history.append(prepared)

    existing_history = (
        list(existing.get("history") or []) if isinstance(existing, Mapping) else []
    )
    existing_manifest = (
        existing.get(EXTERNAL_CONVERSATION_IMPORT_FIELD)
        if isinstance(existing, Mapping)
        else None
    )
    if existing is not None and not isinstance(existing_manifest, Mapping):
        raise ChatHistoryServiceError(
            "The deterministic import session ID collides with a native conversation."
        )
    if isinstance(existing_manifest, Mapping) and (
        existing_manifest.get("source_identity_key")
        != stored_manifest.get("source_identity_key")
    ):
        raise ChatHistoryServiceError(
            "The deterministic import session ID belongs to another external source."
        )
    if isinstance(existing_manifest, Mapping) and bool(
        existing_manifest.get("read_only") is True
        and existing_manifest.get("source_sha256")
        == stored_manifest.get("source_sha256")
        and existing_manifest.get("package_sha256")
        == stored_manifest.get("package_sha256")
        and existing_manifest.get("parser_version")
        == stored_manifest.get("parser_version")
    ):
        existing_synchronisation = existing_manifest.get("synchronisation")
        existing_divergence = (
            existing_synchronisation.get("divergence")
            if isinstance(existing_synchronisation, Mapping)
            else None
        )
        return {
            "success": True,
            "action": "unchanged",
            "session_id": session_id,
            "appended_message_count": 0,
            "divergence": existing_divergence,
            "canonical_read_back": {
                "session_id": session_id,
                "external_conversation": _external_conversation_summary_from_doc(
                    existing
                ),
            },
        }

    divergence: Dict[str, Any] | None = None
    appended_history = prepared_history
    action = "new"
    if existing is not None:
        existing_events = [
            entry
            for entry in existing_history
            if isinstance(entry, Mapping)
            and isinstance(entry.get("external_event_id"), str)
        ]
        existing_by_id = {
            str(entry["external_event_id"]): entry for entry in existing_events
        }
        incoming_by_id = {
            str(entry["external_event_id"]): entry for entry in prepared_history
        }
        existing_ids = list(existing_by_id)
        incoming_known_ids = [
            event_id for event_id in incoming_ids if event_id in existing_by_id
        ]
        missing_ids = [
            event_id for event_id in existing_ids if event_id not in incoming_by_id
        ]
        changed_ids: list[str] = []
        for event_id in existing_ids:
            incoming_entry = incoming_by_id.get(event_id)
            if incoming_entry is None:
                continue
            existing_entry = existing_by_id[event_id]
            existing_fingerprint = existing_entry.get("external_event_sha256")
            if not existing_fingerprint:
                compatible_existing_entry = dict(existing_entry)
                if "external_source_timestamp_utc" not in compatible_existing_entry:
                    compatible_existing_entry["external_source_timestamp_utc"] = (
                        incoming_entry.get("external_source_timestamp_utc")
                    )
                existing_fingerprint = _event_fingerprint(
                    compatible_existing_entry
                )
            if existing_fingerprint != incoming_entry.get("external_event_sha256"):
                changed_ids.append(event_id)
        expected_known_order = [
            event_id for event_id in existing_ids if event_id in incoming_by_id
        ]
        reordered = incoming_known_ids != expected_known_order
        appended_history = [
            entry
            for entry in prepared_history
            if entry["external_event_id"] not in existing_by_id
        ]
        if missing_ids or changed_ids or reordered:
            divergence = {
                "schema_version": "external_conversation_divergence.v1",
                "detected_at_utc": now.isoformat(),
                "missing_event_count": len(missing_ids),
                "changed_event_count": len(changed_ids),
                "reordered": reordered,
                "sample_missing_event_ids": missing_ids[:20],
                "sample_changed_event_ids": changed_ids[:20],
                "existing_history_preserved": True,
            }
        if appended_history:
            action = "appended_with_divergence" if divergence else "appended"
        elif divergence:
            action = "diverged"
        else:
            action = "refreshed"

    combined_history = [*existing_history, *appended_history]
    stored_manifest["synchronisation"] = {
        "schema_version": "external_conversation_synchronisation.v1",
        "action": action,
        "checked_at_utc": now.isoformat(),
        "appended_message_count": len(appended_history),
        "retained_message_count": len(existing_history),
        "divergence": divergence,
    }
    search_segments = [
        segment
        for index, entry in enumerate(combined_history)
        for segment in [_conversation_search_segment(entry, history_index=index)]
        if segment is not None
    ]
    message_times = [
        timestamp
        for entry in combined_history
        for timestamp in [_coerce_datetime(entry.get("timestamp"))]
        if timestamp is not None
    ]
    first_message_at = min(message_times) if message_times else now
    last_message_at = max(message_times) if message_times else now
    set_fields: Dict[str, Any] = {
        EXTERNAL_CONVERSATION_IMPORT_FIELD: stored_manifest,
        "origin_kind": "external_conversation_import",
        "updated_at": last_message_at,
        "conversation_search_segments": search_segments,
        "conversation_search_index_version": CONVERSATION_SEARCH_INDEX_VERSION,
        "conversation_search_indexed_at": now,
    }
    if normalised_name and (
        action == "new"
        or not _normalise_session_name((existing or {}).get("session_name"))
    ):
        set_fields["session_name"] = normalised_name
        set_fields["conversation_search_title"] = normalised_name
    if isinstance(namespace, str) and namespace.strip():
        set_fields["namespace"] = namespace.strip()
    if isinstance(organisation_concept_id, str) and organisation_concept_id.strip():
        set_fields["organisation_concept_id"] = organisation_concept_id.strip()
    if isinstance(role_in_org, str) and role_in_org.strip():
        set_fields["role_in_org"] = role_in_org.strip()

    set_on_insert: Dict[str, Any] = {"created_at": first_message_at}
    update: Dict[str, Any] = {"$set": set_fields, "$setOnInsert": set_on_insert}
    query = dict(exact_query)
    upsert = existing is None
    if existing is None:
        set_fields["history"] = prepared_history
    else:
        query[f"{EXTERNAL_CONVERSATION_IMPORT_FIELD}.package_sha256"] = (
            existing_manifest.get("package_sha256")
        )
        if appended_history:
            update["$push"] = {"history": {"$each": appended_history}}
    try:
        result = chat_history_coll.update_one(
            query,
            update,
            upsert=upsert,
        )
    except PyMongoError as exc:
        if isinstance(exc, DuplicateKeyError) and _concurrency_attempt < 2:
            return upsert_external_conversation_projection(
                user_id=user_id,
                session_id=session_id,
                session_name=session_name,
                namespace=namespace,
                organisation_concept_id=organisation_concept_id,
                role_in_org=role_in_org,
                history=history,
                manifest=manifest,
                _concurrency_attempt=_concurrency_attempt + 1,
            )
        logger.error("Error storing external conversation projection: %s", exc)
        raise ChatHistoryServiceError(
            f"Could not store external conversation projection: {exc}"
        ) from exc
    if not (
        getattr(result, "matched_count", 0)
        or getattr(result, "upserted_id", None) is not None
    ):
        if _concurrency_attempt < 2:
            return upsert_external_conversation_projection(
                user_id=user_id,
                session_id=session_id,
                session_name=session_name,
                namespace=namespace,
                organisation_concept_id=organisation_concept_id,
                role_in_org=role_in_org,
                history=history,
                manifest=manifest,
                _concurrency_attempt=_concurrency_attempt + 1,
            )
        raise ChatHistoryServiceError(
            "External conversation projection was not stored."
        )

    canonical = get_external_conversation_projection(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_history=False,
    )
    canonical_summary = _external_conversation_summary_from_doc(canonical or {})
    if (
        not isinstance(canonical_summary, dict)
        or canonical_summary.get("source_sha256")
        != stored_manifest.get("source_sha256")
        or canonical_summary.get("package_sha256")
        != stored_manifest.get("package_sha256")
    ):
        raise ChatHistoryServiceError(
            "External conversation canonical read-back did not match the import."
        )
    return {
        "success": True,
        "action": action,
        "session_id": session_id,
        "appended_message_count": len(appended_history),
        "divergence": divergence,
        "canonical_read_back": {
            "session_id": session_id,
            "session_name": (canonical or {}).get("session_name"),
            "namespace": (canonical or {}).get("namespace"),
            "external_conversation": canonical_summary,
        },
    }


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


def get_chat_history_session_message_count(
    user_id: str,
    session_id: str,
    *,
    namespace: str,
) -> Optional[int]:
    """Return the exact session's non-reset message count without loading history."""

    user_id_value = _safe_str(user_id)
    session_id_value = _safe_str(session_id)
    namespace_value = _safe_str(namespace)
    if not user_id_value or not session_id_value:
        raise ChatHistoryServiceError("user_id and session_id are required.")
    if not namespace_value:
        raise ChatHistoryServiceError("namespace is required.")

    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    _guard_chat_history_read("get_chat_history_session_message_count")
    query = build_chat_history_query(
        user_id=user_id_value,
        session_id=session_id_value,
        namespace=namespace_value,
        include_legacy=False,
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
                                "$or": [
                                    {"$ne": ["$$msg.role", "system"]},
                                    {"$ne": ["$$msg.content", "__RESET__"]},
                                ]
                            },
                        }
                    }
                },
            }
        },
        {"$limit": 1},
    ]

    try:
        summary = next(
            _read_aggregate(
                chat_history_coll,
                pipeline,
                operation="get_chat_history_session_message_count.aggregate",
            ),
            None,
        )
        if summary is None:
            _record_chat_history_read_success()
            return None
        raw_count = summary.get("message_count") if isinstance(summary, dict) else None
        if (
            not isinstance(raw_count, int)
            or isinstance(raw_count, bool)
            or raw_count < 0
        ):
            raise ChatHistoryServiceError(
                "Chat history session returned an invalid message count."
            )
        _record_chat_history_read_success()
        return raw_count
    except PyMongoError as e:
        _record_chat_history_read_failure(
            "get_chat_history_session_message_count", e
        )
        logger.exception(
            "Error retrieving chat history session message count: %s",
            e,
        )
        raise ChatHistoryServiceError(
            f"Could not retrieve chat history session message count: {e}"
        ) from e


def delete_chat_history(
    user_id: str,
    session_id: str,
    *,
    namespace: str,
) -> Dict[str, Any]:
    """Delete one exact namespaced chat session and read back its absence."""

    user_id_value = _safe_str(user_id)
    session_id_value = _safe_str(session_id)
    namespace_value = _safe_str(namespace)
    if not user_id_value or not session_id_value:
        raise ChatHistoryServiceError("user_id and session_id are required.")
    if not namespace_value:
        raise ChatHistoryServiceError("namespace is required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    query = build_chat_history_query(
        user_id=user_id_value,
        session_id=session_id_value,
        namespace=namespace_value,
        include_legacy=False,
    )
    try:
        result = chat_history_coll.delete_one(query)
        acknowledged = bool(getattr(result, "acknowledged", True))
        raw_deleted_count = getattr(result, "deleted_count", 0)
        deleted_count = (
            raw_deleted_count
            if isinstance(raw_deleted_count, int)
            and not isinstance(raw_deleted_count, bool)
            and raw_deleted_count >= 0
            else 0
        )
        canonical_absent = (
            _read_find_one(
                chat_history_coll,
                query,
                {"_id": 1},
                operation="delete_chat_history.canonical_readback",
            )
            is None
        )
        receipt = {
            "acknowledged": acknowledged,
            "deleted_count": deleted_count,
            "canonical_absent": canonical_absent,
        }
        logger.info(
            "Chat history deletion receipt for user %s, session %s: %s",
            user_id_value,
            session_id_value,
            receipt,
        )
        return receipt
    except PyMongoError as e:
        logger.exception("Error deleting chat history: %s", e)
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


def get_chat_history_sessions_older_than_count(
    user_id: str,
    *,
    cutoff: datetime,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    agent_visibility: Any = CHAT_SESSION_AGENT_VISIBILITY_INCLUDE,
) -> int:
    """Count actor-owned, untrashed sessions older than a recency cutoff.

    This deliberately counts through a small metadata query instead of deriving
    the value from the bounded session-tab result window.  The UI can therefore
    explain that older conversations exist without materialising their history.
    """

    if not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(cutoff, datetime):
        raise ChatHistoryServiceError("cutoff must be a datetime.")

    cutoff_utc = cutoff
    if cutoff_utc.tzinfo is None:
        cutoff_utc = cutoff_utc.replace(tzinfo=timezone.utc)
    else:
        cutoff_utc = cutoff_utc.astimezone(timezone.utc)

    visibility = _normalise_chat_session_agent_visibility(agent_visibility)
    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    _guard_chat_history_read("get_chat_history_sessions_older_than_count")

    query = build_chat_history_query(
        user_id=user_id,
        namespace=namespace,
        include_legacy=include_legacy,
    )
    query["trashed_at"] = None
    clauses: list[Dict[str, Any]] = [
        {
            "$or": [
                {"updated_at": {"$lt": cutoff_utc}},
                {
                    "updated_at": {"$in": [None, ""]},
                    "created_at": {"$lt": cutoff_utc},
                },
            ]
        }
    ]
    agent_created_clause: Dict[str, Any] = {
        "$or": [
            {
                "is_agent_created": {
                    "$in": [True, "true", "1", "yes", "on", "y"]
                }
            },
            {"origin_kind": {"$in": sorted(CHAT_SESSION_AGENT_CREATED_ORIGIN_KINDS)}},
            {
                "test_artifact_kind": {
                    "$exists": True,
                    "$nin": [None, "", " "],
                }
            },
        ]
    }
    if visibility == CHAT_SESSION_AGENT_VISIBILITY_EXCLUDE:
        clauses.append({"$nor": [agent_created_clause]})
    elif visibility == CHAT_SESSION_AGENT_VISIBILITY_ONLY:
        clauses.append(agent_created_clause)
    query["$and"] = clauses

    try:
        summary = next(
            _read_aggregate(
                chat_history_coll,
                [{"$match": query}, {"$count": "session_count"}],
                operation="get_chat_history_sessions_older_than_count.aggregate",
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
        _record_chat_history_read_failure(
            "get_chat_history_sessions_older_than_count", e
        )
        logger.error(
            "Error retrieving older chat history session count: %s", e, exc_info=True
        )
        raise ChatHistoryServiceError(
            f"Could not retrieve older chat history session count: {e}"
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
    focus = _focus_projection_from_doc(doc)
    created_ts = _infer_created_timestamp(doc)
    last_ts = _coerce_datetime(doc.get("updated_at")) or created_ts

    # Metadata-only summaries intentionally avoid history reads for resilience.
    if (
        not session_name
        and not focus["focal_concept_ids"]
        and last_ts is None
        and created_ts is None
    ):
        return None

    provenance = _session_provenance_from_doc(doc)
    is_completed, completed_at_dt = _concept_q_and_a_completion(doc)
    return {
        "session_id": session_id,
        "session_name": session_name,
        "message_count": None,
        "last_message_at": last_ts.isoformat() if last_ts else None,
        "is_completed": is_completed,
        "completed_at": completed_at_dt.isoformat() if completed_at_dt else None,
        "created_at": created_ts.isoformat() if created_ts else None,
        "namespace": doc.get("namespace"),
        "organisation_concept_id": doc.get("organisation_concept_id"),
        "trashed_at": (
            doc.get("trashed_at").isoformat()
            if isinstance(doc.get("trashed_at"), datetime)
            else doc.get("trashed_at")
        ),
        "trashed": doc.get("trashed_at") is not None,
        "conversation_search_index_version": doc.get(
            "conversation_search_index_version"
        ),
        "preview": None,
        **focus,
        **provenance,
        **_concept_q_and_a_metadata_field(doc),
        **_session_external_projection_from_doc(doc),
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
            "trashed_at": 1,
            "conversation_search_index_version": 1,
            "focal_concept_ids": 1,
            "focal_concept_ids_source": 1,
            "focal_concept_ids_updated_at": 1,
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


def get_chat_history_session_summaries_by_ids(
    user_id: str,
    session_ids: Iterable[str],
    *,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """Read a bounded set of metadata summaries in one owner-scoped query."""

    if not isinstance(user_id, str) or not user_id.strip():
        raise ChatHistoryServiceError("user_id is required.")
    unique_session_ids = list(
        dict.fromkeys(
            value.strip()
            for value in session_ids
            if isinstance(value, str) and value.strip()
        )
    )
    if not unique_session_ids:
        return {}
    if len(unique_session_ids) > 500:
        raise ChatHistoryServiceError(
            "A batched conversation summary read is limited to 500 session IDs."
        )
    collection = get_chat_history_collection_service(read_only=True)
    if collection is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")
    query = build_chat_history_query(
        user_id=user_id.strip(), namespace=namespace, include_legacy=include_legacy
    )
    query["session_id"] = {"$in": unique_session_ids}
    try:
        rows = _get_chat_history_session_summaries_metadata_only(
            collection, query=query, safe_limit=len(unique_session_ids)
        )
    except PyMongoError as exc:
        raise ChatHistoryServiceError(
            f"Could not retrieve batched conversation summaries: {exc}"
        ) from exc
    return {
        str(row["session_id"]): row
        for row in rows
        if isinstance(row.get("session_id"), str)
    }


def get_chat_history_session_summaries_page(
    user_id: str,
    *,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    page_size: int = 100,
    position: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return one bounded keyset page of owner-scoped session metadata.

    The source ordering uses the effective session timestamp and ``session_id``
    as a deterministic tie-breaker.  This primitive deliberately pages the
    source collection rather than truncating an in-memory candidate window.
    """

    if not isinstance(user_id, str) or not user_id.strip():
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(page_size, int) or isinstance(page_size, bool):
        raise ChatHistoryServiceError("page_size must be an integer.")
    safe_page_size = max(1, min(page_size, 100))
    collection = get_chat_history_collection_service(read_only=True)
    if collection is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    _guard_chat_history_read("get_chat_history_session_summaries_page")
    base_query = build_chat_history_query(
        user_id=user_id.strip(),
        namespace=namespace,
        include_legacy=include_legacy,
    )
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    pipeline: List[Dict[str, Any]] = [
        {"$match": base_query},
        {
            "$set": {
                "_conversation_page_timestamp": {
                    "$ifNull": ["$updated_at", {"$ifNull": ["$created_at", epoch]}]
                }
            }
        },
    ]
    if position is not None:
        if not isinstance(position, Mapping):
            raise ChatHistoryServiceError("conversation page position is invalid.")
        marker_timestamp = _coerce_datetime(position.get("timestamp"))
        marker_session_id = position.get("session_id")
        if marker_timestamp is None or not isinstance(marker_session_id, str):
            raise ChatHistoryServiceError("conversation page position is incomplete.")
        pipeline.append(
            {
                "$match": {
                    "$or": [
                        {
                            "_conversation_page_timestamp": {
                                "$lt": marker_timestamp
                            }
                        },
                        {
                            "_conversation_page_timestamp": marker_timestamp,
                            "session_id": {"$lt": marker_session_id},
                        },
                    ]
                }
            }
        )
    projection = _add_chat_session_provenance_projection(
        {
            "_id": 0,
            "session_id": 1,
            "session_name": 1,
            "created_at": 1,
            "updated_at": 1,
            "namespace": 1,
            "organisation_concept_id": 1,
            "trashed_at": 1,
            "conversation_search_index_version": 1,
            "focal_concept_ids": 1,
            "focal_concept_ids_source": 1,
            "focal_concept_ids_updated_at": 1,
            "_conversation_page_timestamp": 1,
        }
    )
    pipeline.extend(
        [
            {
                "$sort": {
                    "_conversation_page_timestamp": DESCENDING,
                    "session_id": DESCENDING,
                }
            },
            {"$limit": safe_page_size + 1},
            {"$project": projection},
        ]
    )
    try:
        docs = [
            dict(row)
            for row in _read_aggregate(
                collection,
                pipeline,
                operation="get_chat_history_session_summaries_page.aggregate",
            )
            if isinstance(row, Mapping)
        ]
        _record_chat_history_read_success()
    except PyMongoError as exc:
        _record_chat_history_read_failure(
            "get_chat_history_session_summaries_page", exc
        )
        raise ChatHistoryServiceError(
            f"Could not page chat history session summaries: {exc}"
        ) from exc

    has_more = len(docs) > safe_page_size
    page_docs = docs[:safe_page_size]
    sessions = [
        summary
        for doc in page_docs
        if (summary := _build_session_summary_from_metadata(doc)) is not None
    ]
    next_position = None
    if page_docs:
        final_doc = page_docs[-1]
        final_timestamp = _coerce_datetime(
            final_doc.get("_conversation_page_timestamp")
        ) or epoch
        next_position = {
            "timestamp": final_timestamp.isoformat(),
            "session_id": str(final_doc.get("session_id") or ""),
        }
    return {
        "sessions": sessions,
        "count": len(sessions),
        "page_size": safe_page_size,
        "has_more": has_more,
        "next_position": next_position,
    }


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
                "trashed_at": 1,
                "conversation_search_index_version": 1,
                "session_name": 1,
                "focal_concept_ids": 1,
                "focal_concept_ids_source": 1,
                "focal_concept_ids_updated_at": 1,
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
            focus = _focus_projection_from_doc(doc)
            non_reset = list(_iter_non_reset_messages(history))
            has_messages = bool(non_reset)
            if not has_messages and not session_name and not focus["focal_concept_ids"]:
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
            qa_completed, qa_completed_at = _concept_q_and_a_completion(doc)
            if qa_completed:
                is_completed = True
                completed_at_dt = qa_completed_at or completed_at_dt

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
                    "trashed_at": (
                        doc.get("trashed_at").isoformat()
                        if isinstance(doc.get("trashed_at"), datetime)
                        else doc.get("trashed_at")
                    ),
                    "trashed": doc.get("trashed_at") is not None,
                    "conversation_search_index_version": doc.get(
                        "conversation_search_index_version"
                    ),
                    "preview": preview,
                    **focus,
                    **provenance,
                    **_concept_q_and_a_metadata_field(doc),
                    **_session_external_projection_from_doc(doc),
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
            "focal_concept_ids": 1,
            "focal_concept_ids_source": 1,
            "focal_concept_ids_updated_at": 1,
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
            "focal_concept_ids": 1,
            "focal_concept_ids_source": 1,
            "focal_concept_ids_updated_at": 1,
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
    focus = _focus_projection_from_doc(doc)
    non_reset = list(_iter_non_reset_messages(history))
    has_messages = bool(non_reset)
    if not has_messages and not session_name and not focus["focal_concept_ids"]:
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
    qa_completed, qa_completed_at = _concept_q_and_a_completion(doc)
    if qa_completed:
        is_completed = True
        completed_at_dt = qa_completed_at or completed_at_dt

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
        **focus,
        **provenance,
        **_concept_q_and_a_metadata_field(doc),
        **_session_external_projection_from_doc(doc),
    }
    _record_chat_history_read_success()
    return summary


def create_chat_session(
    *,
    user_id: str,
    session_id: str,
    session_name: Optional[str] = None,
    namespace: Any = _SESSION_CONTEXT_UNSET,
    organisation_concept_id: Any = _SESSION_CONTEXT_UNSET,
    role_in_org: Any = _SESSION_CONTEXT_UNSET,
    mode: Optional[str] = None,
    origin_kind: Optional[str] = None,
    created_by_actor_concept_id: Optional[str] = None,
    created_by_actor_type: Optional[str] = None,
    is_agent_created: Optional[bool] = None,
    test_artifact_kind: Optional[str] = None,
    focal_concept_ids: Any = None,
    concept_q_and_a_state: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a new chat session document if it does not already exist.

    Args:
        user_id: The user concept ID
        session_id: The session UUID
        session_name: Optional display name for the session
        namespace: Optional explicit namespace (e.g., from window session context)
        organisation_concept_id: Optional explicit org ID (e.g., from window session context)
        role_in_org: Optional explicit role (e.g., from window session context)
        mode: Optional durable conversation mode, such as concept_q_and_a
        origin_kind: Optional durable provenance kind for non-human/test sessions
        created_by_actor_concept_id: Optional Vontology actor concept attribution
        created_by_actor_type: Optional actor type concept, such as #V#coding_agent
        is_agent_created: Optional explicit test/agent-created flag
        test_artifact_kind: Optional test/benchmark fixture class
        concept_q_and_a_state: Initial state for a concept_q_and_a mode carrier.
            It is inserted atomically with the session so its active key can be
            protected by the partial unique index.

    Omitted namespace/org/role values fall back to Flask session context.
    Explicit ``None`` values remain authoritative so a Personal window cannot
    inherit an organisation or role from another tab's Flask session.
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
    effective_namespace = (
        session_context.get("namespace")
        if namespace is _SESSION_CONTEXT_UNSET
        else namespace
    )
    effective_org = (
        session_context.get("organisation_concept_id")
        if organisation_concept_id is _SESSION_CONTEXT_UNSET
        else organisation_concept_id
    )
    effective_role = (
        session_context.get("role_in_org")
        if role_in_org is _SESSION_CONTEXT_UNSET
        else role_in_org
    )

    # Derive namespace from context if not explicitly provided
    ns = effective_namespace
    if not ns:
        ns = _derive_rag_namespace(
            session_context={"organisation_concept_id": effective_org}, user_id=user_id
        )
    name = _normalise_session_name(session_name)
    normalised_focus = normalise_focal_concept_ids(focal_concept_ids)
    normalised_mode = _normalise_session_provenance_text(
        mode,
        max_len=80,
        identifier=True,
    )

    set_on_insert: Dict[str, Any] = {
        "created_at": now,
        "updated_at": now,
        "history": [],
    }
    if normalised_mode:
        set_on_insert["mode"] = normalised_mode
    if name:
        set_on_insert["session_name"] = name
        set_on_insert["conversation_search_title"] = name
        set_on_insert["conversation_search_index_version"] = (
            CONVERSATION_SEARCH_INDEX_VERSION
        )
        set_on_insert["conversation_search_indexed_at"] = now
    if normalised_focus:
        set_on_insert["focal_concept_ids"] = normalised_focus
        set_on_insert["focal_concept_ids_source"] = "conversation_launch"
        set_on_insert["focal_concept_ids_updated_at"] = now
    if concept_q_and_a_state is not None:
        if normalised_mode != CHAT_SESSION_MODE_CONCEPT_Q_AND_A:
            raise ChatHistoryServiceError(
                "concept_q_and_a_state requires concept_q_and_a mode."
            )
        if not isinstance(concept_q_and_a_state, Mapping):
            raise ChatHistoryServiceError("concept_q_and_a_state must be an object.")
        set_on_insert["concept_q_and_a"] = dict(concept_q_and_a_state)
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
            {
                "session_name": 1,
                "namespace": 1,
                "focal_concept_ids": 1,
                "focal_concept_ids_source": 1,
                "focal_concept_ids_updated_at": 1,
            }
        )
        doc = chat_history_coll.find_one(
            {"user_id": user_id, "session_id": session_id},
            projection,
        )
        stored_provenance = _session_provenance_from_doc(doc or {})
        return {
            "session_id": session_id,
            "session_name": _normalise_session_name((doc or {}).get("session_name")),
            "namespace": (doc or {}).get("namespace") or ns,
            "mode": _normalise_session_provenance_text(
                (doc or {}).get("mode") or normalised_mode,
                max_len=80,
                identifier=True,
            ),
            **_focus_projection_from_doc(doc),
            **stored_provenance,
            **_session_external_projection_from_doc(doc or {}),
        }
    except DuplicateKeyError:
        # Feature-specific partial unique indexes use this as their atomic
        # concurrency signal; callers must be able to read the winning row.
        raise
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
    require_unnamed: bool = False,
    expected_updated_at: Optional[str] = None,
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
        conditions: List[Dict[str, Any]] = [query]
        if require_unnamed:
            conditions.append(
                {
                    "$or": [
                        {"session_name": None},
                        {"session_name": {"$exists": False}},
                        {"session_name": ""},
                    ]
                }
            )
        if expected_updated_at is not None:
            expected_timestamp = _coerce_datetime(expected_updated_at)
            if expected_timestamp is None:
                raise ChatHistoryServiceError("expected_updated_at is invalid.")
            conditions.append({"updated_at": expected_timestamp})
        if len(conditions) > 1:
            query = {"$and": conditions}
        result = chat_history_coll.update_one(
            query,
            {
                "$set": {
                    "session_name": new_name,
                    "conversation_search_title": new_name,
                    "conversation_search_indexed_at": datetime.now(timezone.utc),
                }
            },
        )
        updated = bool(getattr(result, "modified_count", 0) > 0)
        matched = bool(getattr(result, "matched_count", 0) > 0)
        return {"updated": updated, "matched": matched, "session_name": new_name}
    except PyMongoError as e:
        logger.error(f"Error renaming chat session: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not rename chat session: {e}") from e


def set_chat_session_trashed(
    *,
    user_id: str,
    session_id: str,
    trashed: bool,
    actor_user_id: str,
    namespace: Optional[str] = None,
    organisation_concept_id: Optional[str] = None,
    include_legacy: bool = False,
    verify_active_work: bool = True,
) -> Dict[str, Any]:
    """Move an owner-scoped conversation to or from recoverable Trash."""

    if not isinstance(trashed, bool):
        raise ChatHistoryServiceError("trashed must be a boolean.")
    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if actor_user_id != user_id:
        raise ChatHistoryServiceError("Only the conversation owner can change Trash state.")
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
        before = chat_history_coll.find_one(query, {"_id": 1, "trashed_at": 1})
        if not isinstance(before, Mapping):
            return {"matched": False, "changed": False, "trashed": trashed}
        was_trashed = before.get("trashed_at") is not None
        changed = was_trashed != trashed
        active_work = {
            "policy": "block_trash_while_prompt_is_queued_or_in_progress",
            "status": "not_applicable" if not trashed else "clear",
            "active_queue_ids": [],
        }
        if changed and trashed and verify_active_work:
            try:
                from . import chat_prompt_queue_service

                queue_scope = chat_prompt_queue_service.build_queue_scope(
                    user_concept_id=actor_user_id,
                    organisation_concept_id=organisation_concept_id,
                    namespace=namespace,
                )
                active_records = chat_prompt_queue_service.list_active_queue_records(
                    scope=queue_scope, limit=500
                )
            except Exception as exc:  # noqa: BLE001 - mutation fails closed.
                raise ChatHistoryServiceError(
                    "Could not verify queued or running work before moving the conversation to Trash."
                ) from exc
            matching_records = [
                row
                for row in active_records
                if isinstance(row, Mapping) and row.get("session_id") == session_id
            ]
            if matching_records:
                active_work = {
                    "policy": "block_trash_while_prompt_is_queued_or_in_progress",
                    "status": "blocked",
                    "active_queue_ids": [
                        str(row.get("queue_id"))
                        for row in matching_records
                        if row.get("queue_id")
                    ],
                }
                raise ConversationActiveWorkError(
                    "conversation_has_active_work: finish or cancel queued/running prompts before moving this conversation to Trash."
                )
        if changed:
            if trashed:
                update = {
                    "$set": {
                        "trashed_at": datetime.now(timezone.utc),
                        "trashed_by_actor_user_id": actor_user_id,
                    }
                }
            else:
                update = {
                    "$unset": {
                        "trashed_at": "",
                        "trashed_by_actor_user_id": "",
                    }
                }
            result = chat_history_coll.update_one(query, update)
            if getattr(result, "acknowledged", True) is False:
                raise ChatHistoryServiceError("Conversation Trash update was not acknowledged.")
        read_back = chat_history_coll.find_one(
            query,
            {
                "_id": 0,
                "session_id": 1,
                "session_name": 1,
                "namespace": 1,
                "organisation_concept_id": 1,
                "created_at": 1,
                "updated_at": 1,
                "trashed_at": 1,
            },
        )
        if not isinstance(read_back, Mapping):
            raise ChatHistoryServiceError("Conversation Trash canonical read-back failed.")
        is_trashed = read_back.get("trashed_at") is not None
        if is_trashed != trashed:
            raise ChatHistoryServiceError(
                "Conversation Trash canonical read-back did not match the update."
            )
        return {
            "matched": True,
            "changed": changed,
            "trashed": is_trashed,
            "effect_id": f"conversation_lifecycle:{uuid.uuid4()}",
            "effect_status": "succeeded",
            "active_work": active_work,
            "index_reconciliation": {
                "status": "succeeded",
                "lexical_visibility": (
                    "trash_only" if is_trashed else "active"
                ),
                "semantic_visibility": "canonical_reauthorisation_required",
                "index_version": CONVERSATION_SEARCH_INDEX_VERSION,
            },
            "sharing_impact": {
                "owner_carrier_visibility": (
                    "trash_only" if is_trashed else "active"
                ),
                "accepted_invites_preserved": True,
                "shared_participant_active_visibility_may_change": changed,
            },
            "related_records": {
                "canonical_tasks_preserved": True,
                "canonical_effects_preserved": True,
                "conversation_provenance_preserved": True,
            },
            "canonical_read_back": _build_session_summary_from_metadata(
                dict(read_back)
            ),
        }
    except PyMongoError as exc:
        raise ChatHistoryServiceError(
            f"Could not update conversation Trash state: {exc}"
        ) from exc


def backfill_conversation_search_index(
    *,
    user_concept_id: Optional[str] = None,
    namespace: Optional[str] = None,
    max_sessions: int = 500,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Build the sanitised title/content lexical projection for older sessions."""

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")
    safe_limit = max(1, min(int(max_sessions), 5_000))
    query: Dict[str, Any] = {}
    if isinstance(user_concept_id, str) and user_concept_id.strip():
        query["user_id"] = user_concept_id.strip()
    if isinstance(namespace, str) and namespace.strip():
        query["namespace"] = namespace.strip()
    query["$or"] = [
        {"conversation_search_index_version": {"$ne": CONVERSATION_SEARCH_INDEX_VERSION}},
        {"conversation_search_index_version": {"$exists": False}},
    ]
    examined = 0
    updated = 0
    cursor = chat_history_coll.find(
        query,
        {"_id": 1, "session_name": 1, "history": 1},
    ).limit(safe_limit)
    try:
        cursor = cursor.batch_size(min(safe_limit, 100))
    except Exception:
        pass
    try:
        for doc in cursor:
            if not isinstance(doc, Mapping):
                continue
            examined += 1
            safe_segments = [
                segment
                for history_index, message in enumerate(doc.get("history") or [])
                if isinstance(message, Mapping)
                and (
                    segment := _conversation_search_segment(
                        message, history_index=history_index
                    )
                )
                is not None
            ]
            set_fields: Dict[str, Any] = {
                "conversation_search_text": [
                    segment["text"] for segment in safe_segments
                ],
                "conversation_search_segments": safe_segments,
                "conversation_search_index_version": CONVERSATION_SEARCH_INDEX_VERSION,
                "conversation_search_indexed_at": datetime.now(timezone.utc),
            }
            title = _normalise_session_name(doc.get("session_name"))
            if title:
                set_fields["conversation_search_title"] = title
            if not dry_run:
                result = chat_history_coll.update_one(
                    {"_id": doc.get("_id")}, {"$set": set_fields}
                )
                if getattr(result, "acknowledged", True) is False:
                    raise ChatHistoryServiceError(
                        "Conversation search backfill update was not acknowledged."
                    )
            updated += 1
    except PyMongoError as exc:
        raise ChatHistoryServiceError(
            f"Conversation search index backfill failed: {exc}"
        ) from exc
    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "sessions_examined": examined,
        "sessions_updated": updated,
        "limit": safe_limit,
        "limit_reached": examined >= safe_limit,
        "index_version": CONVERSATION_SEARCH_INDEX_VERSION,
    }


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


def normalise_focal_concept_ids(values: Any) -> List[str]:
    """Return the bounded, ordered focal-object contract for one conversation.

    This is deliberately separate from ``session_links``.  Links are optional
    organisational metadata, whereas focal IDs select the source-backed
    objects that must be re-authorised and hydrated for every ordinary turn.
    """

    return _normalise_concept_id_list(values)[:_MAX_FOCAL_CONCEPT_IDS]


def _focus_projection_from_doc(doc: Mapping[str, Any] | None) -> Dict[str, Any]:
    source = doc if isinstance(doc, Mapping) else {}
    updated_at = _coerce_datetime(source.get("focal_concept_ids_updated_at"))
    return {
        "focal_concept_ids": normalise_focal_concept_ids(
            source.get("focal_concept_ids")
        ),
        "focal_concept_ids_source": (
            str(source.get("focal_concept_ids_source")).strip()
            if isinstance(source.get("focal_concept_ids_source"), str)
            and str(source.get("focal_concept_ids_source")).strip()
            else None
        ),
        "focal_concept_ids_updated_at": (
            updated_at.isoformat() if updated_at is not None else None
        ),
    }


def get_chat_session_focus(
    *,
    user_id: str,
    session_id: str,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
) -> Dict[str, Any]:
    """Read source-owned focus without altering conversation recency."""

    if not isinstance(user_id, str) or not user_id.strip():
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ChatHistoryServiceError("session_id is required.")
    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")
    query = build_chat_history_query(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_legacy=include_legacy,
    )
    try:
        doc = chat_history_coll.find_one(
            query,
            {
                "focal_concept_ids": 1,
                "focal_concept_ids_source": 1,
                "focal_concept_ids_updated_at": 1,
            },
        )
    except PyMongoError as exc:
        raise ChatHistoryServiceError(
            f"Could not read conversation focus: {exc}"
        ) from exc
    return _focus_projection_from_doc(doc)


def set_chat_session_focus(
    *,
    user_id: str,
    session_id: str,
    focal_concept_ids: Any,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    source: str = "conversation_launch",
) -> Dict[str, Any]:
    """Set source-owned focus without changing transcript recency."""

    if not isinstance(user_id, str) or not user_id.strip():
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ChatHistoryServiceError("session_id is required.")
    normalised = normalise_focal_concept_ids(focal_concept_ids)
    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")
    query = build_chat_history_query(
        user_id=user_id,
        session_id=session_id,
        namespace=namespace,
        include_legacy=include_legacy,
    )
    now = datetime.now(timezone.utc)
    try:
        result = chat_history_coll.update_one(
            query,
            {
                "$set": {
                    "focal_concept_ids": normalised,
                    "focal_concept_ids_source": source,
                    "focal_concept_ids_updated_at": now,
                }
            },
        )
    except PyMongoError as exc:
        raise ChatHistoryServiceError(
            f"Could not set conversation focus: {exc}"
        ) from exc
    return {
        "matched": bool(getattr(result, "matched_count", 0) > 0),
        "updated": bool(getattr(result, "modified_count", 0) > 0),
        "focal_concept_ids": normalised,
        "focal_concept_ids_source": source,
        "focal_concept_ids_updated_at": now.isoformat(),
    }


def list_chat_sessions_for_focal_concept(
    *,
    user_id: str,
    focal_concept_id: str,
    namespace: Optional[str] = None,
    limit: int = 25,
) -> List[Dict[str, Any]]:
    """Return recent source-owned sessions focussed on one exact concept ID."""

    focal_ids = normalise_focal_concept_ids([focal_concept_id])
    if not focal_ids:
        raise ChatHistoryServiceError("focal_concept_id is required.")
    safe_limit = min(max(int(limit or 25), 1), 100)
    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    _guard_chat_history_read("list_chat_sessions_for_focal_concept")
    query = build_chat_history_query(
        user_id=user_id,
        namespace=namespace,
        include_legacy=True,
    )
    query["focal_concept_ids"] = focal_ids[0]
    projection = _add_chat_session_provenance_projection(
        {
            "_id": 0,
            "session_id": 1,
            "session_name": 1,
            "created_at": 1,
            "updated_at": 1,
            "namespace": 1,
            "organisation_concept_id": 1,
            "focal_concept_ids": 1,
            "focal_concept_ids_source": 1,
            "focal_concept_ids_updated_at": 1,
        }
    )
    try:
        cursor = _read_find(
            chat_history_coll,
            query,
            projection,
            operation="list_chat_sessions_for_focal_concept.find",
        )
        try:
            cursor = cursor.sort(
                [("updated_at", DESCENDING), ("created_at", DESCENDING)]
            )
        except AttributeError:
            pass
        try:
            cursor = cursor.limit(safe_limit)
        except AttributeError:
            pass
        summaries = [
            summary
            for doc in cursor
            if isinstance(doc, dict)
            and (summary := _build_session_summary_from_metadata(doc)) is not None
        ]
        _record_chat_history_read_success()
        return summaries[:safe_limit]
    except PyMongoError as exc:
        _record_chat_history_read_failure("list_chat_sessions_for_focal_concept", exc)
        raise ChatHistoryServiceError(
            f"Could not list conversations for focal concept: {exc}"
        ) from exc


def get_chat_history_session_summaries_for_owner_sessions(
    owner_session_pairs: Iterable[tuple[Any, Any]], *, limit: int = 50
) -> Dict[tuple[str, str], Dict[str, Any]]:
    """Read accepted-shared session metadata in one bounded exact query.

    Invite acceptance supplies the `(owner, session)` candidate set.  This is
    intentionally a source-store helper rather than a discovery API: it never
    scans by session ID alone and returns no transcript content.
    """

    safe_limit = min(max(int(limit or 50), 1), 50)
    pairs: list[tuple[str, str]] = []
    for raw_owner_id, raw_session_id in owner_session_pairs:
        if not isinstance(raw_owner_id, str) or not isinstance(raw_session_id, str):
            continue
        owner_id = raw_owner_id.strip()
        session_id = raw_session_id.strip()
        if not owner_id or not session_id or (owner_id, session_id) in pairs:
            continue
        pairs.append((owner_id, session_id))
        if len(pairs) == safe_limit:
            break
    if not pairs:
        return {}
    chat_history_coll = get_chat_history_collection_service(read_only=True)
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")
    _guard_chat_history_read("get_chat_history_session_summaries_for_owner_sessions")
    projection = _add_chat_session_provenance_projection(
        {
            "_id": 0,
            "user_id": 1,
            "session_id": 1,
            "session_name": 1,
            "created_at": 1,
            "updated_at": 1,
            "namespace": 1,
            "organisation_concept_id": 1,
            "trashed_at": 1,
            "conversation_search_index_version": 1,
            "focal_concept_ids": 1,
            "focal_concept_ids_source": 1,
            "focal_concept_ids_updated_at": 1,
        }
    )
    try:
        cursor = _read_find(
            chat_history_coll,
            {
                "$or": [
                    {"user_id": owner_id, "session_id": session_id}
                    for owner_id, session_id in pairs
                ]
            },
            projection,
            operation="get_chat_history_session_summaries_for_owner_sessions.find",
        )
        result: Dict[tuple[str, str], Dict[str, Any]] = {}
        wanted = set(pairs)
        for document in cursor:
            if not isinstance(document, dict):
                continue
            owner_id = document.get("user_id")
            session_id = document.get("session_id")
            if not isinstance(owner_id, str) or not isinstance(session_id, str):
                continue
            key = (owner_id, session_id)
            if key not in wanted:
                continue
            summary = _build_session_summary_from_metadata(document)
            if summary is not None:
                result[key] = summary
        _record_chat_history_read_success()
        return result
    except PyMongoError as exc:
        _record_chat_history_read_failure(
            "get_chat_history_session_summaries_for_owner_sessions", exc
        )
        raise ChatHistoryServiceError(
            f"Could not list accepted shared chat sessions: {exc}"
        ) from exc


def _assistant_opening_result_from_doc(
    doc: Mapping[str, Any] | None,
) -> Dict[str, Any] | None:
    if not isinstance(doc, Mapping):
        return None
    opening = doc.get("assistant_opening")
    if not isinstance(opening, Mapping):
        return None
    initiation_id = opening.get("initiation_id")
    if not isinstance(initiation_id, str) or not initiation_id:
        return None

    response_text = opening.get("response_text")
    if not isinstance(response_text, str):
        response_text = None
    history = doc.get("history")
    if response_text is None and isinstance(history, list):
        for message in reversed(history):
            if (
                not isinstance(message, Mapping)
                or message.get("role") != "assistant"
                or message.get("initiation_id") != initiation_id
            ):
                continue
            content = message.get("content")
            if isinstance(content, str):
                response_text = content
                break

    if opening.get("status") == "completed" or response_text is not None:
        return {
            "status": "completed",
            "initiation_id": initiation_id,
            "response_text": response_text,
        }
    return {
        "status": str(opening.get("status") or "in_progress"),
        "initiation_id": initiation_id,
    }


def claim_chat_session_assistant_opening(
    *,
    user_id: str,
    session_id: str,
    initiation_id: str,
    namespace: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically claim the single assistant-first opening for a session."""

    if not initiation_id or len(initiation_id) > 200:
        raise ChatHistoryServiceError("initiation_id is required.")
    coll = get_chat_history_collection_service()
    if coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")
    session_query = build_chat_history_query(
        user_id=user_id, session_id=session_id, namespace=namespace
    )
    now = datetime.now(timezone.utc)
    try:
        existing = coll.find_one(
            session_query,
            {"assistant_opening": 1, "history": {"$slice": -20}},
        )
        if not isinstance(existing, Mapping):
            return {"status": "missing"}
        existing_result = _assistant_opening_result_from_doc(existing)
        if existing_result is not None:
            if existing_result["status"] == "completed":
                # Recover a completed durable assistant message if the final
                # state update failed after persistence.
                opening = existing.get("assistant_opening")
                if (
                    isinstance(opening, Mapping)
                    and opening.get("status") != "completed"
                ):
                    coll.update_one(
                        session_query,
                        {
                            "$set": {
                                "assistant_opening.status": "completed",
                                "assistant_opening.completed_at": now,
                                "assistant_opening.response_text": existing_result.get(
                                    "response_text"
                                ),
                            }
                        },
                    )
                return existing_result
            if existing_result["status"] == "failed":
                if existing_result.get("initiation_id") != initiation_id:
                    return {
                        "status": "already_claimed",
                        "initiation_id": existing_result.get("initiation_id"),
                    }
                retry_query = {
                    "$and": [
                        session_query,
                        {
                            "assistant_opening.initiation_id": initiation_id,
                            "assistant_opening.status": "failed",
                        },
                    ]
                }
                result = coll.update_one(
                    retry_query,
                    {
                        "$set": {
                            "assistant_opening.status": "in_progress",
                            "assistant_opening.started_at": now,
                        },
                        "$unset": {
                            "assistant_opening.failed_at": "",
                            "assistant_opening.failure_kind": "",
                        },
                    },
                )
                if getattr(result, "matched_count", 0):
                    return {"status": "claimed", "initiation_id": initiation_id}
            if existing_result.get("initiation_id") == initiation_id:
                return {
                    "status": "in_progress",
                    "initiation_id": initiation_id,
                }
            return {
                "status": "already_claimed",
                "initiation_id": existing_result.get("initiation_id"),
            }

        claim_query = {
            "$and": [
                session_query,
                {
                    "$or": [
                        {"assistant_opening": {"$exists": False}},
                        {"assistant_opening": None},
                    ]
                },
            ]
        }
        result = coll.update_one(
            claim_query,
            {
                "$set": {
                    "assistant_opening": {
                        "initiation_id": initiation_id,
                        "status": "in_progress",
                        "started_at": now,
                    }
                }
            },
        )
    except PyMongoError as exc:
        raise ChatHistoryServiceError(
            f"Could not claim assistant opening: {exc}"
        ) from exc
    if getattr(result, "matched_count", 0):
        return {"status": "claimed", "initiation_id": initiation_id}

    # Another request may have won the atomic claim between our read and write.
    try:
        current = coll.find_one(
            session_query,
            {"assistant_opening": 1, "history": {"$slice": -20}},
        )
    except PyMongoError as exc:
        raise ChatHistoryServiceError(
            f"Could not reconcile assistant opening claim: {exc}"
        ) from exc
    current_result = _assistant_opening_result_from_doc(current)
    if current_result is None:
        return {"status": "missing"}
    if current_result["status"] == "completed":
        return current_result
    if current_result.get("initiation_id") == initiation_id:
        return {"status": "in_progress", "initiation_id": initiation_id}
    return {
        "status": "already_claimed",
        "initiation_id": current_result.get("initiation_id"),
    }


def complete_chat_session_assistant_opening(
    *,
    user_id: str,
    session_id: str,
    initiation_id: str,
    response_text: Optional[str] = None,
    namespace: Optional[str] = None,
) -> bool:
    """Mark an opening complete only after its assistant message is durable."""

    coll = get_chat_history_collection_service()
    if coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")
    query = build_chat_history_query(
        user_id=user_id, session_id=session_id, namespace=namespace
    )
    query.update(
        {
            "assistant_opening.initiation_id": initiation_id,
            "assistant_opening.status": "in_progress",
        }
    )
    try:
        completed_fields: Dict[str, Any] = {
            "assistant_opening.status": "completed",
            "assistant_opening.completed_at": datetime.now(timezone.utc),
        }
        if isinstance(response_text, str):
            completed_fields["assistant_opening.response_text"] = response_text
        result = coll.update_one(
            query,
            {"$set": completed_fields},
        )
    except PyMongoError as exc:
        raise ChatHistoryServiceError(
            f"Could not complete assistant opening: {exc}"
        ) from exc
    return bool(getattr(result, "matched_count", 0))


def fail_chat_session_assistant_opening(
    *,
    user_id: str,
    session_id: str,
    initiation_id: str,
    failure_kind: str,
    namespace: Optional[str] = None,
) -> bool:
    """Make a claimed opening retryable when no assistant message was durable."""

    coll = get_chat_history_collection_service()
    if coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")
    query = build_chat_history_query(
        user_id=user_id, session_id=session_id, namespace=namespace
    )
    query.update(
        {
            "assistant_opening.initiation_id": initiation_id,
            "assistant_opening.status": "in_progress",
        }
    )
    safe_failure_kind = str(failure_kind or "opening_failed").strip()[:120]
    try:
        result = coll.update_one(
            query,
            {
                "$set": {
                    "assistant_opening.status": "failed",
                    "assistant_opening.failed_at": datetime.now(timezone.utc),
                    "assistant_opening.failure_kind": safe_failure_kind,
                }
            },
        )
    except PyMongoError as exc:
        raise ChatHistoryServiceError(f"Could not fail assistant opening: {exc}") from exc
    return bool(getattr(result, "matched_count", 0))


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
