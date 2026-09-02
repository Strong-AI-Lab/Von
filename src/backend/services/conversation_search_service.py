"""Actor-scoped conversation search shared by browser and MCP adapters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pymongo import DESCENDING
from pymongo.errors import PyMongoError

from . import chat_history_service
from .conversation_management_service import (
    ConversationManagementError,
    list_actor_conversations,
    search_conversation_preference_session_ids,
)
from .opaque_cursor_service import (
    OpaqueCursorError,
    decode_opaque_cursor,
    encode_opaque_cursor,
)
from .rag_service import build_rag_retrieval_state, get_rag_service

SEARCH_SCHEMA_VERSION = "conversation_search_result.v1"
_VALID_MATCH_MODES = frozenset({"lexical", "semantic", "hybrid"})
_VALID_SORTS = frozenset({"relevance", "updated"})
_MAX_CANDIDATES = 500
_SEARCH_CURSOR_TTL_SECONDS = 24 * 60 * 60


class ConversationSearchError(RuntimeError):
    """Raised for invalid or unavailable conversation-search operations."""


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConversationSearchError(f"{field} is required.")
    return value.strip()


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _row_timestamp(row: Mapping[str, Any]) -> str:
    return str(row.get("last_message_at") or row.get("created_at") or "")


def _bounded_snippet(text: Any, query: str, *, max_chars: int = 240) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    cleaned = " ".join(text.split())
    lowered = cleaned.casefold()
    terms = [term.casefold() for term in query.split() if term.strip()]
    offsets = [lowered.find(term) for term in terms]
    offsets = [offset for offset in offsets if offset >= 0]
    centre = min(offsets) if offsets else 0
    start = max(0, centre - max_chars // 3)
    end = min(len(cleaned), start + max_chars)
    snippet = cleaned[start:end]
    if start > 0:
        snippet = f"...{snippet}"
    if end < len(cleaned):
        snippet = f"{snippet}..."
    return snippet


def _matches_filters(row: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    timestamp = _row_timestamp(row)
    date_from = filters.get("date_from")
    if (
        isinstance(date_from, str)
        and date_from.strip()
        and timestamp < date_from.strip()
    ):
        return False
    date_to = filters.get("date_to")
    if isinstance(date_to, str) and date_to.strip() and timestamp > date_to.strip():
        return False
    focal = filters.get("focal_concept_id")
    if isinstance(focal, str) and focal.strip():
        focal_ids = row.get("focal_concept_ids")
        if not isinstance(focal_ids, list) or focal.strip() not in focal_ids:
            return False
    access_mode = filters.get("access_mode")
    if access_mode == "owner" and row.get("shared_with_me") is True:
        return False
    if access_mode == "invitee" and row.get("shared_with_me") is not True:
        return False
    for key in ("hidden", "pinned", "trashed"):
        expected = filters.get(key)
        if isinstance(expected, bool) and (row.get(key) is True) != expected:
            return False
    name_present = filters.get("name_present")
    if isinstance(name_present, bool):
        has_name = bool(str(row.get("session_name") or "").strip())
        if has_name != name_present:
            return False
    return True


def _aggregate_retrieval_status(
    *, rows: list[dict[str, Any]], states: list[dict[str, Any]], limited: bool
) -> str:
    if limited or any(state.get("status") == "unavailable" for state in states):
        return "partial_results" if rows else "unavailable"
    return "results_available" if rows else "valid_empty"


def _lexical_candidates(
    *,
    actor_user_id: str,
    namespace: str | None,
    query: str,
    trashed_only: bool = False,
    candidate_limit: int = _MAX_CANDIDATES,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    collection = chat_history_service.get_chat_history_collection_service(
        read_only=True
    )
    if collection is None:
        return [], {"status": "unavailable", "cause": "chat_history_unavailable"}
    mongo_query = chat_history_service.build_chat_history_query(
        user_id=actor_user_id,
        namespace=namespace,
        include_legacy=False,
    )
    mongo_query["trashed_at"] = (
        {"$exists": True, "$ne": None} if trashed_only else None
    )
    mongo_query["$text"] = {"$search": query}
    projection = {
        "_id": 0,
        "session_id": 1,
        "conversation_search_title": 1,
        "conversation_search_text": 1,
        "conversation_search_segments": 1,
        "score": {"$meta": "textScore"},
    }
    try:
        cursor = collection.find(mongo_query, projection)
        try:
            cursor = cursor.sort(
                [("score", {"$meta": "textScore"}), ("updated_at", DESCENDING)]
            )
        except (AttributeError, TypeError):
            cursor = collection.find(mongo_query, projection)
        cursor = cursor.limit(max(1, min(candidate_limit, _MAX_CANDIDATES)))
        rows = [dict(row) for row in cursor if isinstance(row, Mapping)]
        return rows, {
            "status": "results_available" if rows else "valid_empty",
            "result_count": len(rows),
            "index_version": chat_history_service.CONVERSATION_SEARCH_INDEX_VERSION,
        }
    except PyMongoError as exc:
        return [], {
            "status": "unavailable",
            "cause": f"lexical_query_failed:{type(exc).__name__}",
        }


def _semantic_candidates(
    *,
    actor_user_id: str,
    organisation_concept_id: str | None,
    namespace: str | None,
    query: str,
    hybrid: bool,
    candidate_limit: int = _MAX_CANDIDATES,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    safe_candidate_limit = max(1, min(candidate_limit, _MAX_CANDIDATES))
    try:
        results = get_rag_service().query(
            query,
            top_k=safe_candidate_limit,
            namespace=namespace,
            hybrid=hybrid,
            permissions_context={
                "mode": "chat",
                "user_id": actor_user_id,
                "organisation_concept_id": organisation_concept_id,
                "retrieval_candidate_limit": safe_candidate_limit,
            },
        )
        state = getattr(results, "retrieval_state", None)
        if not isinstance(state, Mapping):
            state = build_rag_retrieval_state(
                "results_available" if results else "valid_empty",
                result_count=len(results),
            )
        return [dict(row) for row in results if isinstance(row, Mapping)], dict(state)
    except Exception as exc:  # noqa: BLE001 - RAG availability is a typed result.
        return [], build_rag_retrieval_state(
            "unavailable",
            cause=f"semantic_query_failed:{type(exc).__name__}",
            detail="The configured semantic conversation index could not be queried.",
        )


def search_actor_conversations(
    *,
    actor_user_id: str,
    query: str,
    namespace: str | None = None,
    organisation_concept_id: str | None = None,
    match_mode: str = "hybrid",
    filters: Mapping[str, Any] | None = None,
    sort: str = "relevance",
    page_size: int = 20,
    cursor: str | None = None,
    include_hidden: bool = False,
    trashed_only: bool = False,
) -> dict[str, Any]:
    """Search accessible conversations and return a signed keyset continuation."""

    actor = _required_text(actor_user_id, field="actor_user_id")
    query_text = _required_text(query, field="query")
    mode = str(match_mode or "hybrid").strip().lower()
    if mode not in _VALID_MATCH_MODES:
        raise ConversationSearchError(
            f"match_mode must be one of {sorted(_VALID_MATCH_MODES)}."
        )
    sort_mode = str(sort or "relevance").strip().lower()
    if sort_mode not in _VALID_SORTS:
        raise ConversationSearchError(f"sort must be one of {sorted(_VALID_SORTS)}.")
    safe_page_size = max(1, min(int(page_size), 100))
    raw_filters = dict(filters or {})
    if "name_present" in raw_filters and not isinstance(
        raw_filters["name_present"], bool
    ):
        raise ConversationSearchError("name_present must be true or false.")
    clean_filters = {
        key: value
        for key, value in raw_filters.items()
        if key
        in {
            "date_from",
            "date_to",
            "focal_concept_id",
            "access_mode",
            "hidden",
            "pinned",
            "trashed",
            "name_present",
        }
        and value is not None
    }
    if isinstance(clean_filters.get("trashed"), bool):
        trashed_only = clean_filters["trashed"]
    if isinstance(clean_filters.get("hidden"), bool):
        include_hidden = True

    if query_text == "*":
        enumeration_scope = {
            "actor_user_id": actor,
            "namespace": namespace,
            "organisation_concept_id": organisation_concept_id,
            "query": query_text,
            "match_mode": mode,
            "filters": clean_filters,
            "sort": sort_mode,
            "include_hidden": include_hidden,
            "trashed_only": trashed_only,
        }
        cursor_context = hashlib.sha256(
            json.dumps(
                enumeration_scope,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()[:32]
        try:
            enumeration = list_actor_conversations(
                actor_user_id=actor,
                namespace=namespace,
                organisation_concept_id=organisation_concept_id,
                limit=safe_page_size,
                include_hidden=include_hidden,
                include_trashed=trashed_only,
                # The source cursor is already signed and binds the actor,
                # namespace, organisation, and list filters.  Returning it
                # directly avoids a second base64/signature envelope that made
                # the continuation unnecessarily long and fragile for MCP
                # clients to carry between calls.
                cursor=cursor,
                name_present=clean_filters.get("name_present"),
                cursor_context=cursor_context,
            )
        except ConversationManagementError as exc:
            raise ConversationSearchError(str(exc)) from exc
        result_rows = []
        for raw_row in enumeration.get("conversations", []):
            if not isinstance(raw_row, Mapping) or not _matches_filters(
                raw_row, clean_filters
            ):
                continue
            row = dict(raw_row)
            row["match"] = {
                "score": 0.0,
                "fields": ["actor_visible_metadata"],
                "snippet": None,
                "source_locator": None,
            }
            result_rows.append(row)
        source_next_cursor = enumeration.get("next_cursor")
        next_cursor = (
            source_next_cursor
            if isinstance(source_next_cursor, str) and source_next_cursor
            else None
        )
        coverage_complete = enumeration.get("coverage_complete") is True
        return {
            "success": True,
            "schema_version": SEARCH_SCHEMA_VERSION,
            "query": query_text,
            "match_mode": mode,
            "sort": sort_mode,
            "filters": clean_filters,
            "candidate_session_ids": [
                str(row.get("session_id"))
                for row in result_rows
                if isinstance(row.get("session_id"), str)
                and row.get("session_id")
            ],
            "results": result_rows,
            "count": len(result_rows),
            "page_size": safe_page_size,
            "has_more": next_cursor is not None,
            "continuation_cursor": next_cursor,
            "next_cursor": next_cursor,
            "coverage_complete": coverage_complete,
            "ordering": enumeration.get("ordering"),
            "index_coverage": {
                "index_version": chat_history_service.CONVERSATION_SEARCH_INDEX_VERSION,
                "accessible_window_count": len(
                    enumeration.get("conversations", [])
                ),
                "indexed_conversation_count": None,
                "shared_conversation_count": sum(
                    1
                    for row in enumeration.get("conversations", [])
                    if isinstance(row, Mapping)
                    and row.get("shared_with_me") is True
                ),
                "complete_for_accessible_window": coverage_complete,
                "candidate_window_limit": None,
                "candidate_window_complete": coverage_complete,
                "limitations": (
                    [] if coverage_complete else ["source_enumeration_incomplete"]
                ),
            },
            "retrieval": {
                "lexical": {"status": "not_requested"},
                "semantic": {"status": "not_requested"},
            },
            "trashed_only": trashed_only,
        }

    accessible_source_rows: list[dict[str, Any]] = []
    accessible_cursor = None
    accessible_result: dict[str, Any] = {"coverage_complete": False}
    try:
        for _ in range((_MAX_CANDIDATES + 99) // 100):
            accessible_result = list_actor_conversations(
                actor_user_id=actor,
                namespace=namespace,
                organisation_concept_id=organisation_concept_id,
                limit=100,
                include_hidden=include_hidden,
                include_trashed=trashed_only,
                cursor=accessible_cursor,
            )
            accessible_source_rows.extend(
                dict(row)
                for row in accessible_result.get("conversations", [])
                if isinstance(row, Mapping)
            )
            raw_next_cursor = accessible_result.get("next_cursor")
            if not isinstance(raw_next_cursor, str) or not raw_next_cursor:
                accessible_cursor = None
                break
            accessible_cursor = raw_next_cursor
    except ConversationManagementError as exc:
        raise ConversationSearchError(str(exc)) from exc
    accessible_rows = [
        dict(row)
        for row in accessible_source_rows
        if isinstance(row, Mapping)
        and (
            row.get("trashed") is True
            if trashed_only
            else row.get("trashed") is not True
        )
        and _matches_filters(row, clean_filters)
    ]
    accessible_by_session = {
        str(row.get("session_id")): row
        for row in accessible_rows
        if isinstance(row.get("session_id"), str) and row.get("session_id")
    }
    index_generation = hashlib.sha256(
        "\n".join(
            "\x00".join(
                (
                    str(row.get("session_id") or ""),
                    str(row.get("conversation_search_index_version") or ""),
                    _row_timestamp(row),
                    str(row.get("session_name") or ""),
                    str(row.get("trashed") is True),
                )
            )
            for row in sorted(
                accessible_rows, key=lambda item: str(item.get("session_id") or "")
            )
        ).encode("utf-8")
    ).hexdigest()[:20]
    retrieval_scopes: list[tuple[str, str | None]] = [(actor, namespace)]
    for row in accessible_rows:
        if row.get("shared_with_me") is not True:
            continue
        owner_id = row.get("shared_owner_user_id")
        owner_namespace = row.get("namespace")
        if not isinstance(owner_id, str) or not owner_id.strip():
            continue
        scope = (
            owner_id.strip(),
            owner_namespace.strip()
            if isinstance(owner_namespace, str) and owner_namespace.strip()
            else None,
        )
        if scope not in retrieval_scopes:
            retrieval_scopes.append(scope)
    retrieval_scope_limit_reached = len(retrieval_scopes) > 10
    retrieval_scopes = retrieval_scopes[:10]
    per_scope_candidate_limit = max(1, _MAX_CANDIDATES // max(1, len(retrieval_scopes)))

    scores: dict[str, float] = {}
    snippets: dict[str, str] = {}
    matched_fields: dict[str, set[str]] = {}
    lexical_state: dict[str, Any] = {"status": "not_requested"}
    semantic_state: dict[str, Any] = {"status": "not_requested"}

    if query_text == "*":
        for session_id in accessible_by_session:
            scores[session_id] = 0.0
            matched_fields.setdefault(session_id, set()).add("trash_view")

    if query_text != "*" and mode in {"lexical", "hybrid"}:
        lexical_rows: list[dict[str, Any]] = []
        lexical_scope_states: list[dict[str, Any]] = []
        for scope_user_id, scope_namespace in retrieval_scopes:
            scope_rows, scope_state = _lexical_candidates(
                actor_user_id=scope_user_id,
                namespace=scope_namespace,
                query=query_text,
                trashed_only=trashed_only,
                candidate_limit=per_scope_candidate_limit,
            )
            lexical_rows.extend(scope_rows)
            lexical_scope_states.append(scope_state)
        lexical_state = {
            "status": _aggregate_retrieval_status(
                rows=lexical_rows,
                states=lexical_scope_states,
                limited=retrieval_scope_limit_reached,
            ),
            "result_count": len(lexical_rows),
            "scope_count": len(retrieval_scopes),
            "scope_limit_reached": retrieval_scope_limit_reached,
            "scopes": lexical_scope_states,
        }
        for candidate in lexical_rows:
            session_id = candidate.get("session_id")
            if (
                not isinstance(session_id, str)
                or session_id not in accessible_by_session
            ):
                continue
            scores[session_id] = scores.get(session_id, 0.0) + max(
                1.0, _safe_float(candidate.get("score"))
            )
            matched_fields.setdefault(session_id, set()).add("indexed_text")
            title = candidate.get("conversation_search_title")
            if isinstance(title, str) and query_text.casefold() in title.casefold():
                matched_fields[session_id].add("title")
            safe_segments = candidate.get("conversation_search_segments")
            if isinstance(safe_segments, list):
                for segment in safe_segments:
                    if not isinstance(segment, Mapping):
                        continue
                    snippet = _bounded_snippet(segment.get("text"), query_text)
                    if snippet and any(
                        term.casefold() in snippet.casefold()
                        for term in query_text.split()
                    ):
                        snippets.setdefault(session_id, snippet)
                        source_locator = segment.get("source_locator")
                        if isinstance(source_locator, Mapping):
                            accessible_by_session[session_id][
                                "_conversation_search_source_locator"
                            ] = dict(source_locator)
                        break
            else:
                segments = candidate.get("conversation_search_text")
                if isinstance(segments, list):
                    for segment in segments:
                        snippet = _bounded_snippet(segment, query_text)
                        if snippet:
                            snippets.setdefault(session_id, snippet)
                            break
        try:
            override_ids = search_conversation_preference_session_ids(
                actor_user_id=actor, query=query_text, limit=_MAX_CANDIDATES
            )
        except ConversationManagementError:
            override_ids = []
            lexical_state.setdefault("warnings", []).append(
                "display_name_override_index_unavailable"
            )
        for session_id in override_ids:
            if session_id in accessible_by_session:
                scores[session_id] = scores.get(session_id, 0.0) + 8.0
                matched_fields.setdefault(session_id, set()).add(
                    "display_name_override"
                )

        # Shared owner titles and older unprojected titles remain discoverable in
        # the bounded authorised metadata window while coverage is reported.
        terms = [term.casefold() for term in query_text.split() if term.strip()]
        for session_id, row in accessible_by_session.items():
            display_name = str(row.get("session_name") or "")
            if terms and all(term in display_name.casefold() for term in terms):
                scores[session_id] = scores.get(session_id, 0.0) + 6.0
                matched_fields.setdefault(session_id, set()).add("display_name")

    if query_text != "*" and mode in {"semantic", "hybrid"} and not trashed_only:
        semantic_rows: list[dict[str, Any]] = []
        semantic_scope_states: list[dict[str, Any]] = []
        for scope_user_id, scope_namespace in retrieval_scopes:
            scope_rows, scope_state = _semantic_candidates(
                actor_user_id=scope_user_id,
                organisation_concept_id=organisation_concept_id,
                namespace=scope_namespace,
                query=query_text,
                hybrid=mode == "hybrid",
                candidate_limit=per_scope_candidate_limit,
            )
            semantic_rows.extend(scope_rows)
            semantic_scope_states.append(scope_state)
        semantic_state = {
            "status": _aggregate_retrieval_status(
                rows=semantic_rows,
                states=semantic_scope_states,
                limited=retrieval_scope_limit_reached,
            ),
            "result_count": len(semantic_rows),
            "scope_count": len(retrieval_scopes),
            "scope_limit_reached": retrieval_scope_limit_reached,
            "scopes": semantic_scope_states,
            "coverage_complete": bool(semantic_scope_states)
            and all(
                state.get("coverage_complete") is True
                for state in semantic_scope_states
            ),
        }
        for candidate in semantic_rows:
            metadata = candidate.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            if metadata.get("role") not in {"user", "assistant"}:
                continue
            session_id = metadata.get("session_id")
            if (
                not isinstance(session_id, str)
                or session_id not in accessible_by_session
            ):
                continue
            scores[session_id] = scores.get(session_id, 0.0) + max(
                0.001, _safe_float(candidate.get("score"))
            )
            matched_fields.setdefault(session_id, set()).add("semantic_content")
            snippet = _bounded_snippet(candidate.get("text"), query_text)
            if snippet:
                snippets.setdefault(session_id, snippet)
            semantic_locator = {
                key: metadata.get(key)
                for key in ("message_id", "turn_id", "timestamp", "role")
                if metadata.get(key) is not None
            }
            if semantic_locator:
                accessible_by_session[session_id].setdefault(
                    "_conversation_search_source_locator", semantic_locator
                )

    result_rows: list[dict[str, Any]] = []
    for session_id, score in scores.items():
        row = dict(accessible_by_session[session_id])
        row["display_name"] = row.get("session_name")
        row["match"] = {
            "score": round(score, 8),
            "fields": sorted(matched_fields.get(session_id, set())),
            "snippet": snippets.get(session_id),
            "source_locator": row.pop(
                "_conversation_search_source_locator", None
            ),
        }
        result_rows.append(row)

    if sort_mode == "updated":
        result_rows.sort(
            key=lambda row: (_row_timestamp(row), str(row.get("session_id") or "")),
            reverse=True,
        )
    else:
        result_rows.sort(
            key=lambda row: (
                _safe_float((row.get("match") or {}).get("score")),
                _row_timestamp(row),
                str(row.get("session_id") or ""),
            ),
            reverse=True,
        )

    cursor_scope = {
        "actor_user_id": actor,
        "namespace": namespace,
        "organisation_concept_id": organisation_concept_id,
        "query": query_text,
        "match_mode": mode,
        "filters": clean_filters,
        "sort": sort_mode,
        "include_hidden": include_hidden,
        "trashed_only": trashed_only,
        "index_version": chat_history_service.CONVERSATION_SEARCH_INDEX_VERSION,
        "index_generation": index_generation,
    }
    if cursor:
        try:
            cursor_payload = decode_opaque_cursor(
                cursor=cursor, purpose="conversation_search"
            )
        except OpaqueCursorError as exc:
            raise ConversationSearchError(
                f"Invalid conversation search cursor: {exc}"
            ) from exc
        if cursor_payload.get("scope") != cursor_scope:
            raise ConversationSearchError(
                "Invalid conversation search cursor: actor, query, filters, sort, or index version changed."
            )
        position = cursor_payload.get("position")
        if not isinstance(position, Mapping):
            raise ConversationSearchError(
                "Invalid conversation search cursor: position is missing."
            )
        if sort_mode == "updated":
            marker = (
                str(position.get("timestamp") or ""),
                str(position.get("session_id") or ""),
            )
            result_rows = [
                row
                for row in result_rows
                if (_row_timestamp(row), str(row.get("session_id") or "")) < marker
            ]
        else:
            marker = (
                _safe_float(position.get("score")),
                str(position.get("timestamp") or ""),
                str(position.get("session_id") or ""),
            )
            result_rows = [
                row
                for row in result_rows
                if (
                    _safe_float((row.get("match") or {}).get("score")),
                    _row_timestamp(row),
                    str(row.get("session_id") or ""),
                )
                < marker
            ]

    has_more = len(result_rows) > safe_page_size
    page = result_rows[:safe_page_size]
    next_cursor = None
    if has_more and page:
        final_row = page[-1]
        position = {
            "timestamp": _row_timestamp(final_row),
            "session_id": str(final_row.get("session_id") or ""),
        }
        if sort_mode == "relevance":
            position["score"] = _safe_float((final_row.get("match") or {}).get("score"))
        next_cursor = encode_opaque_cursor(
            purpose="conversation_search",
            payload={"scope": cursor_scope, "position": position},
            ttl_seconds=_SEARCH_CURSOR_TTL_SECONDS,
        )

    indexed_count = sum(
        1
        for row in accessible_rows
        if row.get("conversation_search_index_version")
        == chat_history_service.CONVERSATION_SEARCH_INDEX_VERSION
    )
    shared_count = sum(
        1 for row in accessible_rows if row.get("shared_with_me") is True
    )
    limitations = []
    if retrieval_scope_limit_reached:
        limitations.append("shared_conversation_owner_scope_limit_reached")
    accessible_coverage_complete = accessible_result.get("coverage_complete") is True
    if not accessible_coverage_complete:
        limitations.append("accessible_conversation_window_incomplete")
    if mode in {"semantic", "hybrid"} and not semantic_state.get(
        "coverage_complete"
    ):
        limitations.append("semantic_index_coverage_unverified")
    if lexical_state.get("status") in {"unavailable", "partial_results"}:
        limitations.append("lexical_retrieval_incomplete")
    coverage_complete = (
        indexed_count == len(accessible_rows)
        and not retrieval_scope_limit_reached
        and accessible_coverage_complete
        and lexical_state.get("status") not in {"unavailable", "partial_results"}
        and (
            mode == "lexical"
            or semantic_state.get("coverage_complete") is True
        )
    )
    return {
        "success": True,
        "schema_version": SEARCH_SCHEMA_VERSION,
        "query": query_text,
        "match_mode": mode,
        "sort": sort_mode,
        "filters": clean_filters,
        "candidate_session_ids": [
            str(row.get("session_id"))
            for row in page
            if isinstance(row.get("session_id"), str) and row.get("session_id")
        ],
        "results": page,
        "count": len(page),
        "page_size": safe_page_size,
        "has_more": has_more,
        "continuation_cursor": next_cursor,
        "next_cursor": next_cursor,
        "coverage_complete": coverage_complete,
        "ordering": {
            "sort": sort_mode,
            "direction": "descending",
            "tie_breaker": "session_id",
            "consistency": "stateless_keyset_current_index_generation",
        },
        "index_coverage": {
            "index_version": chat_history_service.CONVERSATION_SEARCH_INDEX_VERSION,
            "index_generation": index_generation,
            "accessible_window_count": len(accessible_rows),
            "indexed_conversation_count": indexed_count,
            "shared_conversation_count": shared_count,
            "complete_for_accessible_window": (
                indexed_count == len(accessible_rows)
                and not retrieval_scope_limit_reached
            ),
            "candidate_window_limit": _MAX_CANDIDATES,
            "candidate_window_complete": accessible_coverage_complete,
            "limitations": limitations,
        },
        "retrieval": {
            "lexical": lexical_state,
            "semantic": semantic_state,
        },
        "trashed_only": trashed_only,
    }
