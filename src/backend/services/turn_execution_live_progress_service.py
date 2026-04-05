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
        _TOOL_PROGRESS_SESSION_SCOPE_PREFIX,
        _TOOL_PROGRESS_WINDOW_SCOPE_PREFIX,
        _snapshot_tool_progress_for_request,
    )

    resolved_user = _safe_str(user_concept_id)
    if not resolved_user:
        derived_user, _derived_org = derive_actor_context_from_namespace(namespace)
        resolved_user = _safe_str(derived_user)

    candidate_scope_keys: list[str] = []
    explicit_scope_key = _safe_str(scope_key)
    if explicit_scope_key:
        candidate_scope_keys.append(explicit_scope_key)
    if resolved_user:
        candidate_scope_keys.append(f"user:{resolved_user}")
    normalised_window_session_id = _safe_str(window_session_id)
    if normalised_window_session_id:
        candidate_scope_keys.append(
            f"{_TOOL_PROGRESS_WINDOW_SCOPE_PREFIX}{normalised_window_session_id}"
        )
    normalised_anonymous_session_id = _safe_str(anonymous_session_id)
    if normalised_anonymous_session_id:
        candidate_scope_keys.append(
            f"{_TOOL_PROGRESS_SESSION_SCOPE_PREFIX}{normalised_anonymous_session_id}"
        )

    seen: set[str] = set()
    for candidate_scope in candidate_scope_keys:
        if candidate_scope in seen:
            continue
        seen.add(candidate_scope)
        snapshot = _snapshot_tool_progress_for_request(candidate_scope, request_id_value)
        if isinstance(snapshot, dict):
            payload = dict(snapshot)
            payload["request_id"] = request_id_value
            payload["resolved_scope_key"] = candidate_scope
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
    return None
