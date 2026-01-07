"""Chat history service for persistent conversation storage."""

import hashlib
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Iterable
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import PyMongoError
from ..db.mongo_client import get_db
from ..models.chat_history_model import chat_history_collection_name

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
            query["$or"] = [
                {"namespace": ns},
                {"namespace": {"$exists": False}},
                {"namespace": {"$eq": None}},
                {"namespace": {"$in": ["", " "]}},
            ]
        else:
            query["namespace"] = ns

    return query


class ChatHistoryServiceError(Exception):
    """Exception raised for chat history service errors."""

    pass


def get_chat_history_collection_service():
    """Get the chat_history collection."""
    db = get_db()
    if db is None:
        return None
    coll = db[chat_history_collection_name]
    _ensure_chat_history_indexes(coll)
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

        copied = {
            "role": entry.get("role"),
            "content": entry.get("content"),
            "timestamp": entry.get("timestamp"),
        }
        if include_debug and "llm_debug_data" in entry:
            copied["llm_debug_data"] = entry.get("llm_debug_data")
        existing_location = entry.get("history_location")
        if isinstance(existing_location, dict):
            existing_index = existing_location.get("history_index")
            existing_session = existing_location.get("session_id") or session_id
            if isinstance(existing_index, int) and existing_index >= 0:
                copied["history_location"] = {
                    "session_id": existing_session,
                    "history_index": existing_index,
                }
            else:
                copied["history_location"] = {
                    "session_id": session_id,
                    "history_index": idx,
                }
        else:
            copied["history_location"] = {
                "session_id": session_id,
                "history_index": idx,
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


def get_chat_history(user_id: str, session_id: str) -> List[Dict[str, Any]]:
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

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        doc = chat_history_coll.find_one({"user_id": user_id, "session_id": session_id})

        if doc:
            return doc.get("history", [])
        return []
    except PyMongoError as e:
        logger.error(f"Error retrieving chat history: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not retrieve chat history: {e}") from e


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
        projection = None
        if isinstance(history_tail_limit, int) and history_tail_limit > 0:
            projection = {"history": {"$slice": -history_tail_limit}}
        doc = chat_history_coll.find_one(query, projection)
        if not doc:
            return ([], {"history_truncated": False}) if return_meta else []

        history = doc.get("history") or []
        if not isinstance(history, list) or not history:
            return ([], {"history_truncated": False}) if return_meta else []
        history_truncated = bool(
            isinstance(history_tail_limit, int)
            and history_tail_limit > 0
            and len(history) >= history_tail_limit
        )

        if include_locations:
            segments = _split_history_into_segments_with_locations(
                history, session_id=session_id, include_debug=include_debug
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
        if return_meta:
            return result, {"history_truncated": history_truncated}
        return result
    except PyMongoError as e:
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
) -> Optional[Dict[str, Any]]:
    """Return stored llm_debug_data for a specific history entry."""
    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id:
        raise ChatHistoryServiceError("session_id is required.")
    if not isinstance(history_index, int) or history_index < 0:
        raise ChatHistoryServiceError("history_index must be a non-negative integer.")

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
        projection = {"history": {"$slice": [history_index, 1]}}
        doc = chat_history_coll.find_one(query, projection)
        if not doc:
            return None
        history = doc.get("history") or []
        if not isinstance(history, list) or not history:
            return None
        entry = history[0]
        if not isinstance(entry, dict):
            return None
        debug_data = entry.get("llm_debug_data")
        if not isinstance(debug_data, dict):
            return None
        return debug_data
    except PyMongoError as e:
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


def add_message_to_history(
    user_id: str,
    session_id: str,
    message: Dict[str, Any],
    llm_debug_data: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Adds a message to the chat history for a specific user and session.

    Args:
        user_id: The user's concept ID
        session_id: The session UUID
        message: Message dictionary with 'role' and 'content' keys
        llm_debug_data: Optional LLM debug information (model, context stats, tool usage, etc.)
    """
    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")

    if not message or "role" not in message or "content" not in message:
        raise ChatHistoryServiceError("Message must contain 'role' and 'content' keys.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        # Get session context for organisation/role/namespace (best effort).
        session_context = get_session_context()

        # Determine namespace used for both persistence and RAG indexing.
        ns = _derive_rag_namespace(session_context=session_context, user_id=user_id)

        # Add timestamp to message (and llm_debug_data if present)
        message_with_timestamp = {**message, "timestamp": datetime.now(timezone.utc)}
        if llm_debug_data:
            message_with_timestamp["llm_debug_data"] = llm_debug_data

        set_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
        if isinstance(ns, str) and ns.strip():
            set_fields["namespace"] = ns.strip()

        session_name = None
        if message.get("role") == "user":
            session_name = _session_name_from_message(message.get("content"))

        set_on_insert: Dict[str, Any] = {"created_at": datetime.now(timezone.utc)}
        if session_name:
            set_on_insert["session_name"] = session_name
        org_concept_id = session_context.get("organisation_concept_id")
        if isinstance(org_concept_id, str) and org_concept_id.strip():
            set_on_insert["organisation_concept_id"] = org_concept_id.strip()
        role_in_org = session_context.get("role_in_org")
        if isinstance(role_in_org, str) and role_in_org.strip():
            set_on_insert["role_in_org"] = role_in_org.strip()

        # Update or insert the session document
        result = chat_history_coll.update_one(
            {"user_id": user_id, "session_id": session_id},
            {
                "$push": {"history": message_with_timestamp},
                "$set": set_fields,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )

        logger.debug(
            f"Added message to history for user {user_id}, session {session_id}"
        )

        # Index to RAG (Best effort)
        if get_rag_service:
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
                            session_context=session_context,
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
                                            session_context=session_context,
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

    except PyMongoError as e:
        logger.error(f"Error adding message to history: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not add message to history: {e}") from e


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

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        total_turns = 0
        query = build_chat_history_query(
            user_id=user_id, namespace=namespace, include_legacy=include_legacy
        )
        for doc in chat_history_coll.find(query):
            history = doc.get("history", [])
            if not isinstance(history, list):
                continue
            total_turns += sum(1 for _ in _iter_non_reset_messages(history))
        return total_turns
    except PyMongoError as e:
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

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        count = 0
        query = build_chat_history_query(
            user_id=user_id, namespace=namespace, include_legacy=include_legacy
        )
        for doc in chat_history_coll.find(query, {"history": 1, "session_name": 1}):
            history = doc.get("history") or []
            if not isinstance(history, list):
                history = []
            has_messages = any(True for _ in _iter_non_reset_messages(history))
            session_name = doc.get("session_name")
            has_name = isinstance(session_name, str) and session_name.strip()
            if has_messages or has_name:
                count += 1
        return count
    except PyMongoError as e:
        logger.error(f"Error retrieving chat history session count: {e}", exc_info=True)
        raise ChatHistoryServiceError(
            f"Could not retrieve chat history session count: {e}"
        ) from e


def get_chat_history_session_summaries(
    user_id: str,
    limit: int = 50,
    *,
    namespace: Optional[str] = None,
    include_legacy: bool = True,
    summary_mode: str = "full",
) -> List[Dict[str, Any]]:
    """Return per-session summaries ordered by inferred last message timestamp desc.

    summary_mode="light" avoids loading full histories and omits message_count/preview.
    """
    if not user_id:
        raise ChatHistoryServiceError("user_id is required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    safe_limit = 50
    if isinstance(limit, int) and limit > 0:
        safe_limit = min(limit, 500)

    mode = summary_mode.strip().lower() if isinstance(summary_mode, str) else "full"
    light_mode = mode in ("light", "minimal", "summary")

    try:
        query = build_chat_history_query(
            user_id=user_id, namespace=namespace, include_legacy=include_legacy
        )
        docs: List[Dict[str, Any]]
        history_field = "history"
        if light_mode:
            history_field = "history_tail"
            pipeline = [
                {"$match": query},
                {
                    "$project": {
                        "session_id": 1,
                        "session_name": 1,
                        "created_at": 1,
                        "updated_at": 1,
                        "namespace": 1,
                        "history_tail": {"$slice": ["$history", -1]},
                        "message_count": {
                            "$size": {
                                "$filter": {
                                    "input": "$history",
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
            ]
            docs = list(chat_history_coll.aggregate(pipeline))
        else:
            projection: Dict[str, Any] = {
                "session_id": 1,
                "history": 1,
                "created_at": 1,
                "updated_at": 1,
                "namespace": 1,
                "session_name": 1,
            }
            docs = list(chat_history_coll.find(query, projection))

        summaries: List[Dict[str, Any]] = []
        for doc in docs:
            session_id = doc.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue

            history = doc.get(history_field) or []
            if not isinstance(history, list):
                history = []

            session_name = _normalise_session_name(doc.get("session_name"))
            non_reset = list(_iter_non_reset_messages(history))
            has_messages = bool(history) if light_mode else bool(non_reset)
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

            message_count = None
            preview = None
            if light_mode:
                if isinstance(doc.get("message_count"), int):
                    message_count = doc.get("message_count")
            else:
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
                    "preview": preview,
                }
            )

        summaries.sort(
            key=lambda s: _coerce_datetime(s.get("last_message_at"))
            or datetime(1970, 1, 1, tzinfo=timezone.utc),
            reverse=True,
        )
        return summaries[:safe_limit]
    except PyMongoError as e:
        logger.error(
            f"Error retrieving chat history session summaries: {e}", exc_info=True
        )
        raise ChatHistoryServiceError(
            f"Could not retrieve chat history session summaries: {e}"
        ) from e


def create_chat_session(
    *,
    user_id: str,
    session_id: str,
    session_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new chat session document if it does not already exist."""
    if not isinstance(user_id, str) or not user_id:
        raise ChatHistoryServiceError("user_id is required.")
    if not isinstance(session_id, str) or not session_id:
        raise ChatHistoryServiceError("session_id is required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    now = datetime.now(timezone.utc)
    session_context = get_session_context()
    ns = _derive_rag_namespace(session_context=session_context, user_id=user_id)
    name = _normalise_session_name(session_name) or _default_session_name(now)

    set_on_insert: Dict[str, Any] = {
        "created_at": now,
        "updated_at": now,
        "session_name": name,
    }
    if isinstance(ns, str) and ns.strip():
        set_on_insert["namespace"] = ns.strip()
    org_concept_id = session_context.get("organisation_concept_id")
    if isinstance(org_concept_id, str) and org_concept_id.strip():
        set_on_insert["organisation_concept_id"] = org_concept_id.strip()
    role_in_org = session_context.get("role_in_org")
    if isinstance(role_in_org, str) and role_in_org.strip():
        set_on_insert["role_in_org"] = role_in_org.strip()

    try:
        chat_history_coll.update_one(
            {"user_id": user_id, "session_id": session_id},
            {"$setOnInsert": set_on_insert},
            upsert=True,
        )
        doc = chat_history_coll.find_one(
            {"user_id": user_id, "session_id": session_id},
            {"session_name": 1, "namespace": 1},
        )
        return {
            "session_id": session_id,
            "session_name": _normalise_session_name(
                (doc or {}).get("session_name") or name
            ),
            "namespace": (doc or {}).get("namespace") or ns,
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

    if safe_chunk_start == 0 and _should_skip_rag_reindex(doc, signature, indexable_total):
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
        if previous_failed == 0 and (previous_success + messages_indexed_success) >= indexable_total:
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
