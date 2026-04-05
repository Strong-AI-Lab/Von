"""Server-side builders for compact conversation telemetry locators."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from . import chat_history_service

CONVERSATION_LLM_TELEMETRY_LOCATOR_SCHEMA_VERSION = (
    "conversation_llm_telemetry_locator.v1"
)


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp_to_iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    candidate = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except Exception:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _build_tool_call_descriptor(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    purpose: str | None = None,
) -> dict[str, Any]:
    payload = {
        "tool_name": tool_name,
        "arguments": {
            key: value
            for key, value in arguments.items()
            if value is not None
        },
    }
    if isinstance(purpose, str) and purpose.strip():
        payload["purpose"] = purpose.strip()
    return payload


def _extract_turn_id(message: Mapping[str, Any], llm_debug_data: Mapping[str, Any]) -> str:
    for candidate in (
        llm_debug_data.get("turn_id"),
        message.get("turn_id"),
        message.get("id"),
        message.get("message_id"),
    ):
        cleaned = _safe_str(candidate)
        if cleaned:
            return cleaned
    history_index = _safe_int(message.get("_history_index"))
    if history_index is not None:
        return f"history-assistant-{history_index}"
    return "history-assistant"


def _build_namespace_context(
    *,
    namespace: str | None,
    user_id: str | None,
    organisation_concept_id: str | None,
) -> dict[str, Any]:
    return {
        "namespace": _safe_str(namespace),
        "user_id": _safe_str(user_id),
        "org_id": _safe_str(organisation_concept_id),
    }


def build_conversation_llm_telemetry_locator(
    *,
    user_id: str,
    session_id: str,
    namespace: str | None = None,
    include_legacy: bool = True,
) -> dict[str, Any]:
    """Return a compact MCP-oriented locator for one conversation's turn telemetry."""

    session_id_value = _safe_str(session_id)
    user_id_value = _safe_str(user_id)
    if not session_id_value:
        raise chat_history_service.ChatHistoryServiceError("session_id is required.")
    if not user_id_value:
        raise chat_history_service.ChatHistoryServiceError("user_id is required.")

    session_summary = chat_history_service.get_chat_history_session_summary(
        user_id_value,
        session_id_value,
        namespace=namespace,
        include_legacy=include_legacy,
        summary_mode="light",
    ) or {}

    history = chat_history_service.get_chat_history(user_id_value, session_id_value)
    if not isinstance(history, list):
        history = []

    assistant_transcript_turn_count = 0
    turns_with_history_location_count = 0
    turns_with_request_id_count = 0
    turns_with_unavailable_locator_fields_count = 0
    turns: list[dict[str, Any]] = []

    for history_index, raw_message in enumerate(history):
        if not isinstance(raw_message, Mapping):
            continue
        raw_role = _safe_str(raw_message.get("role")) or _safe_str(
            raw_message.get("sender")
        )
        if raw_role and raw_role.lower() in {"assistant", "von"}:
            assistant_transcript_turn_count += 1

        llm_debug_data = raw_message.get("llm_debug_data")
        if not isinstance(llm_debug_data, Mapping):
            continue

        history_location = {
            "session_id": session_id_value,
            "history_index": history_index,
        }
        turns_with_history_location_count += 1

        request_id = _safe_str(llm_debug_data.get("request_id"))
        if request_id:
            turns_with_request_id_count += 1

        timestamp_utc = (
            _parse_timestamp_to_iso(raw_message.get("timestamp"))
            or _parse_timestamp_to_iso(raw_message.get("created_at"))
            or _parse_timestamp_to_iso(llm_debug_data.get("timestamp_utc"))
        )

        unavailable_locator_fields: list[str] = []
        if not timestamp_utc:
            unavailable_locator_fields.append("timestamp_utc")
        if not request_id:
            unavailable_locator_fields.append("request_id")
        if unavailable_locator_fields:
            turns_with_unavailable_locator_fields_count += 1

        turn_payload: dict[str, Any] = {
            "sequence": len(turns) + 1,
            "turn_id": _extract_turn_id(raw_message, llm_debug_data),
            "timestamp_utc": timestamp_utc,
            "history_location": history_location,
            "request_id": request_id,
            "mcp_access": {
                "chat_history_get_debug_entry": _build_tool_call_descriptor(
                    "chat_history_get_debug_entry",
                    {
                        "session_id": session_id_value,
                        "history_index": history_index,
                        "namespace": namespace,
                    },
                    purpose="Fetch the exact stored llm_debug_data for this history entry.",
                )
            },
        }
        if request_id:
            turn_payload["mcp_access"][
                "turn_execution_get_diagnostics"
            ] = _build_tool_call_descriptor(
                "turn_execution_get_diagnostics",
                {
                    "request_id": request_id,
                    "namespace": namespace,
                },
                purpose=(
                    "Fetch the persisted full turn-execution diagnostics payload for this assistant turn."
                ),
            )
        if unavailable_locator_fields:
            turn_payload["unavailable_locator_fields"] = unavailable_locator_fields
        turns.append(turn_payload)

    metadata = {
        "total_turns": len(turns),
        "llm_debug_turn_count": len(turns),
        "transcript_turn_count": len(history),
        "assistant_transcript_turn_count": assistant_transcript_turn_count,
        "has_partial_telemetry": bool(
            max(0, assistant_transcript_turn_count - len(turns))
            or turns_with_unavailable_locator_fields_count
        ),
        "missing_turn_telemetry_count": max(
            0, assistant_transcript_turn_count - len(turns)
        ),
        "turns_with_history_location_count": turns_with_history_location_count,
        "turns_with_request_id_count": turns_with_request_id_count,
        "turns_with_unavailable_locator_fields_count": (
            turns_with_unavailable_locator_fields_count
        ),
        "ordering": "history_index",
        "history_owner_user_id": user_id_value,
    }

    namespace_context = _build_namespace_context(
        namespace=namespace,
        user_id=user_id_value,
        organisation_concept_id=_safe_str(
            session_summary.get("organisation_concept_id")
        ),
    )

    return {
        "schema_version": CONVERSATION_LLM_TELEMETRY_LOCATOR_SCHEMA_VERSION,
        "generated_at_utc": _now_utc_iso(),
        "session_id": session_id_value,
        "session_name": _safe_str(session_summary.get("session_name")),
        "namespace_context": namespace_context,
        "metadata": metadata,
        "mcp_access": {
            "conversation_telemetry_get_locator": _build_tool_call_descriptor(
                "conversation_telemetry_get_locator",
                {
                    "session_id": session_id_value,
                    "namespace": namespace,
                },
                purpose=(
                    "Rebuild this compact conversation-turn locator from stored chat history."
                ),
            ),
            "chat_history_get_segments": _build_tool_call_descriptor(
                "chat_history_get_segments",
                {
                    "session_id": session_id_value,
                    "namespace": namespace,
                    "include_debug": True,
                },
                purpose="Fetch the stored conversation transcript segments and embedded debug payloads.",
            ),
            "turn_execution_list": _build_tool_call_descriptor(
                "turn_execution_list",
                {
                    "session_id": session_id_value,
                    "namespace": namespace,
                },
                purpose="List turn-execution projections for this conversation.",
            ),
        },
        "turns": turns,
    }
