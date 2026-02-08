# Workflow MCP Capability Matrix (WS4 / JVNAUTOSCI-1089)

## Purpose

This matrix defines where workflow management and chat introspection tools are
available. It prevents ambiguous "missing tool" behaviour across MCP surfaces.

## Server Surfaces

| Surface | Intended usage |
| --- | --- |
| `internal_mcp_gateway` | Canonical workflow management and introspection surface used by Von internals. |
| `vontology_mcp_stdio_server` | Vontology concept/text tools for external IDE agents. Workflow tools are intentionally not exposed directly. |
| `vonrag_mcp_stdio_server` | Focused RAG/search tools only. |

## Tool Availability

| Tool | internal_mcp_gateway | vontology_mcp_stdio_server | vonrag_mcp_stdio_server |
| --- | --- | --- | --- |
| `workflow_list_definitions` | Yes | No | No |
| `workflow_create_instance` | Yes | No | No |
| `workflow_list_instances` | Yes | No | No |
| `workflow_get_instance` | Yes | No | No |
| `workflow_cancel_instance` | Yes | No | No |
| `workflow_retry_instance` | Yes | No | No |
| `workflow_create_schedule` | Yes | No | No |
| `workflow_list_schedules` | Yes | No | No |
| `workflow_get_schedule` | Yes | No | No |
| `workflow_set_schedule_enabled` | Yes | No | No |
| `workflow_delete_schedule` | Yes | No | No |
| `workflow_trigger_schedule` | Yes | No | No |
| `workflow_mcp_health_check` | Yes | No | No |
| `chat_get_prompt_context` | Yes | No | No |
| `chat_introspect` | Yes | No | No |
| `settings_get_public` | Yes | No | No |

## Delegated Access Notes

- `vontology_mcp_stdio_server` can still access workflow behaviour indirectly via
  `von_chat_run` (tool orchestration pathway), but it does not expose direct
  `workflow_*` tool calls.
- `vonrag_mcp_stdio_server` is intentionally limited to RAG/search tools.

## Diagnostics Contract

When a workflow/introspection tool is requested from `vontology_mcp_stdio_server`
and the tool is not exposed on that surface:

- response `error_code` is `unknown_tool`
- response `error_details.surface_diagnostic.classification` is
  `internal_mcp_only_tool`
- response includes actionable guidance and this matrix path.

