"""Shared helpers for surfacing MCP tool results into workflow actions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .action_registry import WorkflowActionResult


def _coerce_mcp_error_message(*, tool_name: str, payload: Mapping[str, Any]) -> str:
    error_text = str(
        payload.get("error")
        or payload.get("error_code")
        or payload.get("message")
        or ""
    ).strip()
    return error_text or f"mcp_tool_error:{tool_name}"


def candidate_internal_mcp_tool_names(action_id: str | None) -> tuple[str, ...]:
    """Return plausible internal MCP tool names for a workflow action ID."""

    action_text = str(action_id or "").strip()
    if not action_text:
        return ()

    candidates: list[str] = [action_text]
    underscored = action_text.replace(".", "_")
    if underscored and underscored not in candidates:
        candidates.append(underscored)
    return tuple(candidates)


def resolve_internal_mcp_tool_name(
    requested_name: str | None,
    *,
    available_tool_names: Sequence[str] | None = None,
) -> str | None:
    """Resolve the best available internal MCP tool name for ``requested_name``."""

    candidates = candidate_internal_mcp_tool_names(requested_name)
    if not candidates:
        return None
    if not available_tool_names:
        return candidates[0]

    available = {
        str(name or "").strip()
        for name in available_tool_names
        if str(name or "").strip()
    }
    for candidate in candidates:
        if candidate in available:
            return candidate
    return None


def workflow_action_result_from_mcp_payload(
    *,
    tool_name: str,
    payload: Any,
    duration_ms: float | None,
) -> WorkflowActionResult:
    """Convert an MCP gateway payload into a workflow action result.

    Internal MCP handlers standardise tool-level errors as mapping payloads with
    ``success=false``. Workflow execution needs those responses to fail closed
    rather than continuing as if the tool succeeded merely because transport
    execution completed.
    """

    outputs = {
        "mcp_result": payload,
        "mcp_tool": tool_name,
        "mcp_duration_ms": duration_ms,
        "result": payload,
    }
    if isinstance(payload, Mapping) and payload.get("success") is False:
        return WorkflowActionResult(
            status="failed",
            error=_coerce_mcp_error_message(tool_name=tool_name, payload=payload),
            outputs=dict(outputs),
            duration_ms=duration_ms,
        )

    return WorkflowActionResult(
        status="success",
        outputs=dict(outputs),
        duration_ms=duration_ms,
    )


__all__ = [
    "candidate_internal_mcp_tool_names",
    "resolve_internal_mcp_tool_name",
    "workflow_action_result_from_mcp_payload",
]
