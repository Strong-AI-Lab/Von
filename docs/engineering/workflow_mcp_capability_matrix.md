# Workflow MCP Capability Matrix (WS4 / JVNAUTOSCI-1089)

## Purpose

This matrix defines where workflow management and chat introspection tools are
available. It prevents ambiguous "missing tool" behaviour across MCP surfaces.

## Server Surfaces

| Surface | Intended usage |
| --- | --- |
| `internal_mcp_gateway` | Canonical workflow management and introspection surface used by Von internals. |
| `vontology_mcp_stdio_server` | Vontology concept/text tools for external IDE agents. Durable `workflow_*` tools are exposed directly; chat introspection tools remain internal-only. |
| `vonrag_mcp_stdio_server` | Focused RAG/search tools only. |

## Tool Availability

| Tool | internal_mcp_gateway | vontology_mcp_stdio_server | vonrag_mcp_stdio_server |
| --- | --- | --- | --- |
| `workflow_list_definitions` | Yes | Yes | No |
| `workflow_bind_event` | Yes | Yes | No |
| `workflow_list_event_bindings` | Yes | Yes | No |
| `workflow_create_instance` | Yes | Yes | No |
| `workflow_list_instances` | Yes | Yes | No |
| `workflow_get_instance` | Yes | Yes | No |
| `workflow_cancel_instance` | Yes | Yes | No |
| `workflow_retry_instance` | Yes | Yes | No |
| `workflow_create_schedule` | Yes | Yes | No |
| `workflow_list_schedules` | Yes | Yes | No |
| `workflow_get_schedule` | Yes | Yes | No |
| `workflow_set_schedule_enabled` | Yes | Yes | No |
| `workflow_delete_schedule` | Yes | Yes | No |
| `workflow_trigger_schedule` | Yes | Yes | No |
| `workflow_mcp_health_check` | Yes | Yes | No |
| `chat_get_prompt_context` | Yes | No | No |
| `chat_introspect` | Yes | No | No |
| `settings_get_public` | Yes | No | No |

## Delegated Access Notes

- `vontology_mcp_stdio_server` exposes direct durable `workflow_*` calls and can
  also access workflow behaviour indirectly via `von_chat_run` (tool orchestration pathway).
- `vonrag_mcp_stdio_server` is intentionally limited to RAG/search tools.

## Diagnostics Contract

When a chat-introspection tool (`chat_get_prompt_context`, `chat_introspect`,
`settings_get_public`) is requested from `vontology_mcp_stdio_server` and the
tool is not exposed on that surface:

- response `error_code` is `unknown_tool`
- response `error_details.surface_diagnostic.classification` is
  `internal_mcp_only_tool`
- response includes actionable guidance and this matrix path.

## Stable Telemetry Fields (WS8)

The following diagnostics fields are treated as stable contracts for workflow
hardening validation and operational troubleshooting.

### `workflow_list_definitions` baseline telemetry

`workflow_list_definitions` returns `baseline_telemetry` with at least:

- `workflow_discovery_executable_checks_total`
- `workflow_discovery_executable_checks_hit`
- `workflow_discovery_executable_hit_ratio`
- `generic_fallback_mcp_invocations_total`
- `generic_fallback_mcp_invocations_success`
- `generic_fallback_mcp_invocations_failed`
- `generic_fallback_mcp_success_ratio`

### Workflow metadata validation events

Workflow engine and durable executor context now records
`workflow_metadata_validation_events` entries with stable keys:

- `status` (`metadata_validation`)
- `state_id`
- `phase` (`pre_action` or `post_action`)
- `ok`
- `applied`
- `mode` (`enforce`, `warn`, `off`)
- `enforced` (`true` only when failures are fail-fast)
- optional failure diagnostics (`reason_code`, `message`, `details`)
- optional skip diagnostics (`skipped`, `skip_reason`)

`last_metadata_validation` mirrors the latest event for quick inspection.

## Rollout Controls and Fallback

High-risk runtime metadata checks are controlled by:

- `VON_WORKFLOW_METADATA_VALIDATION_MODE=enforce|warn|off`

Mode behaviour:

- `enforce` (default): metadata validation failures stop workflow execution.
- `warn`: failures are recorded in telemetry/events but execution continues.
- `off`: metadata checks are skipped for states that declare metadata checks.

Suggested rollback sequence:

1. Set `VON_WORKFLOW_METADATA_VALIDATION_MODE=warn` and restart Von.
2. Confirm diagnostics stream is healthy (`workflow_metadata_validation_events` present).
3. If workflows still degrade, set `VON_WORKFLOW_METADATA_VALIDATION_MODE=off`.
4. After remediation, restore to `enforce`.

