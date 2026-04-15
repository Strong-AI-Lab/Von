"""Read-only access to live turn-progress snapshots stored for active requests."""

from __future__ import annotations

from typing import Any

from .namespace_service import derive_actor_context_from_namespace


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def get_turn_execution_live_progress_payload(
    *,
    request_id: str,
    namespace: str | None = None,
    user_concept_id: str | None = None,
    window_session_id: str | None = None,
    anonymous_session_id: str | None = None,
    scope_key: str | None = None,
) -> dict[str, Any] | None:
    """Return the serialised live progress snapshot for one request if present."""

    request_id_value = _safe_str(request_id)
    if not request_id_value:
        return None

    from ..server.routes.von_routes import (
        _resolve_tool_progress_state_from_scope_candidates,
        _serialise_tool_progress_state,
    )

    resolved_user = _safe_str(user_concept_id)
    if not resolved_user:
        derived_user, _derived_org = derive_actor_context_from_namespace(namespace)
        resolved_user = _safe_str(derived_user)

    explicit_scope_key = _safe_str(scope_key)
    normalised_window_session_id = _safe_str(window_session_id)
    normalised_anonymous_session_id = _safe_str(anonymous_session_id)
    state, resolved_scope_key = _resolve_tool_progress_state_from_scope_candidates(
        request_id=request_id_value,
        explicit_scope_key=explicit_scope_key,
        user_concept_id=resolved_user,
        window_session_id=normalised_window_session_id,
        anonymous_session_id=normalised_anonymous_session_id,
    )
    if not isinstance(state, dict):
        return None

    payload = _serialise_tool_progress_state(state)
    payload["request_id"] = request_id_value
    payload["resolved_scope_key"] = resolved_scope_key
    payload["progress_source"] = "tool_progress_state"
    payload["mcp_access"] = {
        "turn_execution_get_live_progress": {
            "tool_name": "turn_execution_get_live_progress",
            "arguments": {
                "request_id": request_id_value,
                "namespace": _safe_str(namespace),
                "user_concept_id": resolved_user,
                "window_session_id": normalised_window_session_id,
                "anonymous_session_id": normalised_anonymous_session_id,
                "scope_key": explicit_scope_key,
            },
            "purpose": (
                "Fetch the current serialised live progress snapshot for this in-flight turn."
            ),
        }
    }
    return payload
