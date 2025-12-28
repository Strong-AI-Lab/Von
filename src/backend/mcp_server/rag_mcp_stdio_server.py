#!/usr/bin/env python3
"""MCP stdio server for Von RAG operations.

This is a focused MCP server that exposes a small, coherent RAG surface to IDE
agents (e.g. VS Code Copilot) without requiring the full Vontology tool set.

Security note: this server is intended for trusted local development. It still
fails closed on missing `namespace` to reduce accidental cross-namespace access.
"""

import asyncio
import json
import os
import sys
from typing import Any, Awaitable, Callable

# Avoid UnicodeEncodeError on Windows consoles (default cp1252) when any
# dependency logs Unicode. MCP runs over stdio; we must not crash on encode.
try:
    stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
    stderr_reconfigure = getattr(sys.stderr, "reconfigure", None)
    if callable(stdout_reconfigure):
        stdout_reconfigure(encoding="utf-8", errors="backslashreplace")
    if callable(stderr_reconfigure):
        stderr_reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:  # pragma: no cover
    pass

# Adjust path to import from the project root
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Load repo-root .env for MCP stdio runs (VS Code MCP launches may not source .env).
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(dotenv_path=os.path.join(project_root, ".env"), override=False)
except Exception:
    pass

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError:
    print("Error: MCP package not installed. Run: pdm add mcp", file=sys.stderr)
    raise

# Absolute imports (required when executed as a script)
from src.backend.integrations.internal_mcp import catalogue as internal_catalogue


app = Server("vonrag-mcp")


def _json_text(payload: Any) -> TextContent:
    return TextContent(type="text", text=json.dumps(payload, indent=2, default=str))


def _require_namespace(arguments: dict[str, Any]) -> str | None:
    ns = arguments.get("namespace")
    if not isinstance(ns, str) or not ns.strip():
        return None
    return ns.strip()


def _namespace_required_error() -> dict[str, Any]:
    return {
        "success": False,
        "error": "namespace_required",
        "message": "RAG access requires an explicit namespace (e.g. #V#user@org)",
    }


def _tool_handler(
    fn: Callable[..., Any] | Callable[..., Awaitable[Any]],
) -> Callable[[dict[str, Any]], Awaitable[list[TextContent]]]:
    async def _inner(arguments: dict[str, Any]) -> list[TextContent]:
        ns = _require_namespace(arguments)
        if not ns:
            return [_json_text(_namespace_required_error())]

        # Pass through args to the authoritative internal handler.
        maybe_result = fn(**arguments)
        if asyncio.iscoroutine(maybe_result):
            result = await maybe_result
        else:
            result = maybe_result
        return [_json_text(result)]

    return _inner


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="rag_list_collections",
            description=(
                "List available RAG collections/sources for the given namespace. "
                "Use when asked 'what is in my RAG store?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Required namespace (e.g. #V#user@org)",
                    }
                },
                "required": ["namespace"],
            },
        ),
        Tool(
            name="rag_get_status",
            description=(
                "Get RAG status counts for the given namespace (mirrors /admin/rag_status)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Required namespace (e.g. #V#user@org)",
                    },
                    "detail": {
                        "type": "boolean",
                        "description": "If true, return more detailed stats when available",
                        "default": False,
                    },
                },
                "required": ["namespace"],
            },
        ),
        Tool(
            name="rag_list_indexed",
            description=(
                "List indexed sessions for a namespace. Use collection=ka_sessions or collection=chat_history_sessions "
                "to disambiguate."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Required namespace (e.g. #V#user@org)",
                    },
                    "collection": {
                        "type": "string",
                        "description": "Collection selector (ka_sessions|chat_history_sessions)",
                        "default": "ka_sessions",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max items to return",
                        "default": 20,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset",
                        "default": 0,
                    },
                },
                "required": ["namespace"],
            },
        ),
        Tool(
            name="rag_get_item",
            description=(
                "Fetch one indexed session/item by session_id for a namespace. Use collection=... to disambiguate."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Required namespace (e.g. #V#user@org)",
                    },
                    "collection": {
                        "type": "string",
                        "description": "Collection selector (ka_sessions|chat_history_sessions)",
                        "default": "ka_sessions",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session identifier (Mongo _id for KA sessions, session_id for chat sessions)",
                    },
                },
                "required": ["namespace", "session_id"],
            },
        ),
        Tool(
            name="search_knowledge_base",
            description=(
                "Semantic search over the vector-store RAG content for the given namespace. "
                "Returns relevant chunks with provenance-stamped metadata."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Required namespace (e.g. #V#user@org)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query text",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return",
                        "default": 5,
                    },
                },
                "required": ["namespace", "query"],
            },
        ),
    ]


_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[list[TextContent]]]] = {
    "rag_list_collections": _tool_handler(internal_catalogue._rag_list_collections),
    "rag_get_status": _tool_handler(internal_catalogue._rag_get_status),
    "rag_list_indexed": _tool_handler(internal_catalogue._rag_list_indexed),
    "rag_get_item": _tool_handler(internal_catalogue._rag_get_item),
    "search_knowledge_base": _tool_handler(internal_catalogue._search_knowledge_base),
}


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:  # type: ignore[misc]
    handler = _TOOL_HANDLERS.get(name)
    if not handler:
        return [_json_text({"success": False, "error": f"Unknown tool: {name}"})]

    if not isinstance(arguments, dict):
        arguments = {}

    try:
        return await handler(arguments)
    except Exception as exc:  # Defensive: avoid crashing the stdio server
        return [_json_text({"success": False, "error": str(exc)})]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
