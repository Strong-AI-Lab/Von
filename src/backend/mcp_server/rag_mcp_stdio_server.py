#!/usr/bin/env python3
"""MCP stdio server for Von RAG operations.

This is a focused MCP server that exposes a small, coherent RAG surface to IDE
agents (e.g. VS Code Copilot) without requiring the full Vontology tool set.

Security note: this server is intended for trusted local development. It still
fails closed on missing `namespace` to reduce accidental cross-namespace access.
"""

import asyncio
import importlib
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

terminate_duplicate_sibling_servers = importlib.import_module(
    "src.backend.mcp_server.process_guard"
).terminate_duplicate_sibling_servers

# Optional recovery hygiene: enable only when explicitly debugging stale
# sibling MCP processes from previous IDE restarts.
terminate_duplicate_sibling_servers(
    __file__, log_fn=lambda message: print(message, file=sys.stderr)
)

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
internal_catalogue = importlib.import_module(
    "src.backend.integrations.internal_mcp.catalogue"
)
tool_contract_registry_module = importlib.import_module(
    "src.backend.integrations.internal_mcp.tool_contract_registry"
)
SURFACE_VONRAG_STDIO = tool_contract_registry_module.SURFACE_VONRAG_STDIO
get_surface_tool_payloads = tool_contract_registry_module.get_surface_tool_payloads


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


def _tool_from_surface_payload(tool_payload: dict[str, Any]) -> Tool:
    input_schema = tool_payload.get("inputSchema")
    if not isinstance(input_schema, dict):
        input_schema = {}
    return Tool(
        name=str(tool_payload["name"]),
        description=str(tool_payload.get("description") or ""),
        inputSchema=input_schema,
    )


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        _tool_from_surface_payload(tool_payload)
        for tool_payload in get_surface_tool_payloads(SURFACE_VONRAG_STDIO)
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

