# vonrag MCP integration

This repo includes a dedicated MCP stdio server that exposes a small, coherent RAG tool surface to IDE agents (e.g. VS Code Copilot).

## Configuration

The server is registered in `.vscode/mcp.json` as `vonrag`:

- Command: `pdm run python src/backend/mcp_server/rag_mcp_stdio_server.py`
- Working directory: `${workspaceFolder}`

After editing `.vscode/mcp.json`, reload VS Code (or restart the MCP servers) so the new server is discovered.

## Tools

`vonrag` exposes:

- `rag_list_collections` — discover available RAG collections/sources for a namespace
- `rag_get_status` — status counts for a namespace (mirrors `/admin/rag_status`)
- `rag_list_indexed` — list sessions for a namespace; use `collection=ka_sessions` or `collection=chat_history_sessions`
- `rag_get_item` — fetch a single session by `session_id`; use `collection=...` to disambiguate
- `search_knowledge_base` — semantic search over vector-store content for the namespace

## Namespace requirement

All tools require an explicit `namespace` and fail closed when it is missing. This reduces accidental cross-namespace access during local development.

## Notes

- These tools reuse the authoritative internal handlers in `src/backend/integrations/internal_mcp/catalogue.py`, so provenance stamping and collection semantics stay consistent across internal and external tool surfaces.
