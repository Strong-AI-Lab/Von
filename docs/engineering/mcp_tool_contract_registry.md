# MCP Tool Contract Registry

## Purpose

`src/backend/integrations/internal_mcp/tool_contract_registry.py` is the canonical source of truth for MCP tool contracts across Von surfaces.

It removes drift between:

- internal MCP catalogue metadata,
- `mcp_stdio_server.py` `list_tools`,
- `rag_mcp_stdio_server.py` `list_tools`,
- `jira_mcp_server.py` `list_tools` subset, and
- `src/backend/mcp_server/vontology_mcp.json`.

## Surface Model

Each canonical contract declares explicit exposure flags:

- `expose_in_internal_catalogue`
- `expose_in_vontology_stdio`
- `expose_in_vonrag_stdio`
- `expose_in_manifest`
- `expose_in_jira_family_server`

Intentional asymmetries (for example stdio-only compatibility tools) must be declared in the registry as supplemental contracts.

## Change Workflow

When adding or changing MCP tools:

1. Add or update the internal method definition in `src/backend/integrations/internal_mcp/catalogue.py` when the tool belongs to the internal gateway.
2. Update exposure sets in `tool_contract_registry.py` for intended surfaces.
3. If the tool is intentionally surface-only and not in the internal catalogue, add a supplemental contract entry in `tool_contract_registry.py`.
4. Regenerate manifest:
   - `pdm run python scripts/regenerate_vontology_mcp_manifest.py`
5. Run parity tests:
   - `pdm run pytest tests/backend/test_mcp_manifest_parity.py tests/backend/test_mcp_tool_contract_registry.py`

## Guardrails

Parity tests fail when:

- a surface exposes a tool that is not canonically registered,
- the manifest drifts from canonical contracts,
- stdio `list_tools` drifts from canonical contracts, or
- undeclared surface-only exceptions appear.
