"""Shared workflow/introspection MCP surface capability metadata.

This module is the canonical source for workflow/introspection MCP surface
diagnostics. Keep these definitions central so internal gateway handlers,
external stdio diagnostics, and docs stay aligned as tool surfaces evolve.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

# NOTE: Keep this list in sync with workflow tool MethodDefinitions in
# internal_mcp/catalogue.py. Tests verify these names exist to prevent drift.
WORKFLOW_MANAGEMENT_TOOL_NAMES: tuple[str, ...] = (
    "workflow_list_definitions",
    "workflow_validate_candidate",
    "workflow_bind_event",
    "workflow_set_event_binding_enabled",
    "workflow_delete_event_binding",
    "workflow_list_event_bindings",
    "workflow_create_instance",
    "workflow_execute",
    "workflow_list_instances",
    "workflow_list_execution_traces",
    "workflow_build_prediction_envelope",
    "workflow_get_instance",
    "workflow_get_execution_trace",
    "workflow_cancel_instance",
    "workflow_retry_instance",
    "workflow_create_schedule",
    "workflow_list_schedules",
    "workflow_get_schedule",
    "workflow_set_schedule_enabled",
    "workflow_delete_schedule",
    "workflow_trigger_schedule",
)

WORKFLOW_INTROSPECTION_TOOL_NAMES: tuple[str, ...] = (
    "chat_get_prompt_context",
    "chat_introspect",
    "settings_get_public",
)

WORKFLOW_HEALTHCHECK_TOOL_NAMES: tuple[str, ...] = (
    "workflow_mcp_health_check",
    "workflow_materialisation_diagnostics",
    "workflow_concept_parity_audit",
)

WORKFLOW_STDIO_EXPOSED_TOOL_NAMES: tuple[str, ...] = tuple(
    sorted(set(WORKFLOW_MANAGEMENT_TOOL_NAMES) | set(WORKFLOW_HEALTHCHECK_TOOL_NAMES))
)

_DOCS_REFERENCE = "docs/engineering/workflow_mcp_capability_matrix.md"


def tracked_workflow_surface_tool_names() -> tuple[str, ...]:
    """Return the tracked workflow/introspection tool names in stable order."""

    return tuple(
        sorted(
            set(WORKFLOW_MANAGEMENT_TOOL_NAMES)
            | set(WORKFLOW_INTROSPECTION_TOOL_NAMES)
            | set(WORKFLOW_HEALTHCHECK_TOOL_NAMES)
        )
    )


def is_tracked_workflow_surface_tool(tool_name: str | None) -> bool:
    """Return True when the tool is workflow/introspection related for WS4."""

    if not isinstance(tool_name, str):
        return False
    normalised = tool_name.strip()
    if not normalised:
        return False
    return normalised in tracked_workflow_surface_tool_names()


def build_workflow_surface_capability_matrix(
    *, internal_method_names: Iterable[str] | None = None
) -> dict[str, Any]:
    """Build the workflow/introspection MCP capability matrix by server surface."""

    tracked_tools = tracked_workflow_surface_tool_names()
    available_internal = (
        set(str(name) for name in internal_method_names)
        if internal_method_names is not None
        else set(tracked_tools)
    )

    internal_availability = {
        tool_name: tool_name in available_internal for tool_name in tracked_tools
    }
    unavailable_availability = {tool_name: False for tool_name in tracked_tools}
    stdio_availability = {
        tool_name: tool_name in set(WORKFLOW_STDIO_EXPOSED_TOOL_NAMES)
        for tool_name in tracked_tools
    }

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "docs_reference": _DOCS_REFERENCE,
        "tracked_tools": list(tracked_tools),
        "surfaces": {
            "internal_mcp_gateway": {
                "intended_usage": "Canonical workflow management and chat introspection surface inside Von.",
                "tool_availability": internal_availability,
                "delegated_entrypoints": [],
            },
            "vontology_mcp_stdio_server": {
                "intended_usage": (
                    "Vontology concept/text operations for external IDE agents; "
                    "durable workflow tools are exposed directly, while chat "
                    "introspection tools remain internal-only."
                ),
                "tool_availability": stdio_availability,
                "delegated_entrypoints": ["von_chat_run"],
            },
            "vonrag_mcp_stdio_server": {
                "intended_usage": "Focused RAG/search MCP surface.",
                "tool_availability": unavailable_availability,
                "delegated_entrypoints": [],
            },
        },
    }


def classify_stdio_missing_tool(tool_name: str | None) -> dict[str, Any] | None:
    """Return actionable diagnostics for missing workflow/introspection tools."""

    if not is_tracked_workflow_surface_tool(tool_name):
        return None

    requested_tool = str(tool_name).strip()
    if requested_tool in set(WORKFLOW_STDIO_EXPOSED_TOOL_NAMES):
        return None
    matrix = build_workflow_surface_capability_matrix()
    return {
        "classification": "internal_mcp_only_tool",
        "requested_tool": requested_tool,
        "recommended_surface": "internal_mcp_gateway",
        "docs_reference": matrix["docs_reference"],
        "suggestions": [
            f"'{requested_tool}' is not exposed by the vontology MCP stdio server.",
            "Use 'von_chat_run' for delegated workflow access from this stdio surface.",
            "For direct workflow/introspection calls, use the internal MCP gateway.",
            f"See {matrix['docs_reference']} for per-surface tool availability.",
        ],
    }
