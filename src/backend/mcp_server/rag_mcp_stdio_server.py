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
    if isinstance(ns, str) and ns.strip():
        return ns.strip()

    env_ns = os.getenv("VON_DEFAULT_NAMESPACE")
    if isinstance(env_ns, str) and env_ns.strip():
        arguments["namespace"] = env_ns.strip()
        return env_ns.strip()

    return None


def _namespace_required_error() -> dict[str, Any]:
    return {
        "success": False,
        "error": "namespace_required",
        "message": (
            "RAG access requires a namespace. Provide 'namespace' (e.g. #V#user@org) "
            "or set VON_DEFAULT_NAMESPACE in .env"
        ),
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
                        "description": "Optional namespace (e.g. #V#user@org). Defaults to env VON_DEFAULT_NAMESPACE.",
                    }
                },
                "required": [],
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
                        "description": "Optional namespace (e.g. #V#user@org). Defaults to env VON_DEFAULT_NAMESPACE.",
                    },
                    "detail": {
                        "type": "boolean",
                        "description": "If true, return more detailed stats when available",
                        "default": False,
                    },
                },
                "required": [],
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
                        "description": "Optional namespace (e.g. #V#user@org). Defaults to env VON_DEFAULT_NAMESPACE.",
                    },
                    "collection": {
                        "type": "string",
                        "description": "Collection selector (ka_sessions|chat_history_sessions|vontology_text_relations)",
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
                "required": [],
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
                        "description": "Optional namespace (e.g. #V#user@org). Defaults to env VON_DEFAULT_NAMESPACE.",
                    },
                    "collection": {
                        "type": "string",
                        "description": "Collection selector (ka_sessions|chat_history_sessions|vontology_text_relations)",
                        "default": "ka_sessions",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session identifier (Mongo _id for KA sessions, session_id for chat sessions)",
                    },
                },
                "required": ["session_id"],
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
                        "description": "Optional namespace (e.g. #V#user@org). Defaults to env VON_DEFAULT_NAMESPACE.",
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
                    "mode": {
                        "type": "string",
                        "description": "Semantic search mode (chat|concepts|all). Default all.",
                        "default": "all",
                    },
                    "type": {
                        "type": "string",
                        "description": "Filter by document type (chat_message|text_relation).",
                    },
                    "predicate": {
                        "type": "string",
                        "description": "Filter by predicate (e.g. hasDescription).",
                    },
                    "predicates": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by multiple predicates.",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="search_concept_descriptions",
            description=(
                "Semantic search over concept descriptions only (hasDescription text relations) for the given namespace."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Optional namespace (e.g. #V#user@org). Defaults to env VON_DEFAULT_NAMESPACE.",
                    },
                    "query": {"type": "string", "description": "Search query text"},
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return",
                        "default": 5,
                    },
                    "org_id": {
                        "type": "string",
                        "description": "Optional organisation concept id (alias for organisation_concept_id).",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_related_concepts",
            description=(
                "Find concepts with similar descriptions (vector similarity) within the given namespace."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Optional namespace (e.g. #V#user@org). Defaults to env VON_DEFAULT_NAMESPACE.",
                    },
                    "concept_id": {
                        "type": "string",
                        "description": "Concept ID to find related concepts for (e.g. #V#my_concept).",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of related results to return",
                        "default": 10,
                    },
                    "seed_text": {
                        "type": "string",
                        "description": "Optional override for the seed description text.",
                    },
                },
                "required": ["concept_id"],
            },
        ),
        Tool(
            name="index_concept_text",
            description=(
                "Force reindex a single concept's text relations for the given namespace."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Optional namespace (e.g. #V#user@org). Defaults to env VON_DEFAULT_NAMESPACE.",
                    },
                    "concept_id": {
                        "type": "string",
                        "description": "Concept ID to reindex (e.g. #V#my_concept).",
                    },
                    "predicates": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional predicate filter (e.g. hasName, hasDescription)",
                    },
                    "languages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional language filter (e.g. en-NZ)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max relations to consider",
                        "default": 5000,
                    },
                    "batch_size": {
                        "type": "integer",
                        "description": "Upsert batch size",
                        "default": 200,
                    },
                },
                "required": ["concept_id"],
            },
        ),
        Tool(
            name="rag_sync_text_relations",
            description=(
                "Index Vontology text relations (text_relations + text_values) into the vector-store for the given namespace. "
                "After syncing, they become discoverable via search_knowledge_base."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Optional namespace (e.g. #V#user@org). Defaults to env VON_DEFAULT_NAMESPACE.",
                    },
                    "predicates": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional predicate filter (e.g. hasName, hasDescription)",
                    },
                    "languages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional language filter (e.g. en-NZ)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max relations to consider",
                        "default": 5000,
                    },
                    "batch_size": {
                        "type": "integer",
                        "description": "Upsert batch size",
                        "default": 200,
                    },
                },
                "required": [],
            },
        ),
    ]


_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[list[TextContent]]]] = {
    "rag_list_collections": _tool_handler(internal_catalogue._rag_list_collections),
    "rag_get_status": _tool_handler(internal_catalogue._rag_get_status),
    "rag_list_indexed": _tool_handler(internal_catalogue._rag_list_indexed),
    "rag_get_item": _tool_handler(internal_catalogue._rag_get_item),
    "search_knowledge_base": _tool_handler(internal_catalogue._search_knowledge_base),
    "search_concept_descriptions": _tool_handler(
        internal_catalogue._search_concept_descriptions
    ),
    "get_related_concepts": _tool_handler(internal_catalogue._get_related_concepts),
    "index_concept_text": _tool_handler(internal_catalogue._index_concept_text),
    "rag_sync_text_relations": _tool_handler(
        internal_catalogue._rag_sync_text_relations
    ),
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
