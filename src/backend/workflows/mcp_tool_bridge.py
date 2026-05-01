"""Shared helpers for surfacing MCP tool results into workflow actions."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
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


def mcp_input_schema_accepts_namespace(input_schema: Any) -> bool:
    """Return whether an MCP input schema can receive ``namespace``.

    Namespace is runtime tenancy context, not a universal tool argument. Strict
    tool schemas that do not declare it must not receive it, otherwise gateway
    validation fails before the handler can run.
    """

    if input_schema is None:
        return True
    if bool(getattr(input_schema, "allow_unknown", False)):
        return True
    if isinstance(input_schema, Mapping) and (
        bool(input_schema.get("allow_unknown"))
        or bool(input_schema.get("additionalProperties"))
    ):
        return True
    properties = (
        input_schema.get("properties") if isinstance(input_schema, Mapping) else None
    )
    if isinstance(properties, Mapping) and "namespace" in properties:
        return True

    required = (
        input_schema.get("required")
        if isinstance(input_schema, Mapping)
        else getattr(input_schema, "required", None)
    )
    optional = (
        input_schema.get("optional")
        if isinstance(input_schema, Mapping)
        else getattr(input_schema, "optional", None)
    )
    return (
        (isinstance(required, Mapping) and "namespace" in required)
        or (
            isinstance(required, Sequence)
            and not isinstance(required, (str, bytes, bytearray))
            and "namespace" in required
        )
        or (isinstance(optional, Mapping) and "namespace" in optional)
        or (
            isinstance(optional, Sequence)
            and not isinstance(optional, (str, bytes, bytearray))
            and "namespace" in optional
        )
    )


def mcp_method_accepts_namespace(method_definition: Any) -> bool:
    """Return whether a resolved MCP method can receive ``namespace``."""

    if method_definition is None:
        return False
    if isinstance(method_definition, Mapping):
        return mcp_input_schema_accepts_namespace(method_definition.get("input_schema"))
    return mcp_input_schema_accepts_namespace(
        getattr(method_definition, "input_schema", None)
    )


def apply_namespace_to_mcp_payload(
    payload: MutableMapping[str, Any],
    *,
    input_schema: Any,
    user_namespace: str | None,
) -> dict[str, Any] | None:
    """Apply or remove runtime namespace context according to the tool schema."""

    accepts_namespace = mcp_input_schema_accepts_namespace(input_schema)
    if not accepts_namespace:
        if "namespace" not in payload:
            return None
        payload.pop("namespace", None)
        return {
            "field": "namespace",
            "source": "removed_for_strict_tool_schema",
            "value_present": False,
        }

    namespace = str(user_namespace or "").strip() if isinstance(user_namespace, str) else ""
    if namespace and "namespace" not in payload:
        payload["namespace"] = namespace
        return {
            "field": "namespace",
            "source": "user_namespace",
            "value_present": True,
        }
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
    "apply_namespace_to_mcp_payload",
    "candidate_internal_mcp_tool_names",
    "mcp_input_schema_accepts_namespace",
    "mcp_method_accepts_namespace",
    "resolve_internal_mcp_tool_name",
    "workflow_action_result_from_mcp_payload",
]
