"""Shared helpers for surfacing MCP tool results into workflow actions."""

from __future__ import annotations

from collections.abc import Mapping
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


__all__ = ["workflow_action_result_from_mcp_payload"]
