import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.backend.integrations.internal_mcp.mcp_proxy_base import (  # noqa: E402
    MCPServerConfig,
    MCPStdIOClient,
    MCPToolClientError,
)

TOOLS_CACHE_DIR = REPO_ROOT / "data" / "mcp_tool_cache"
DEFAULT_TOOLS_CACHE = TOOLS_CACHE_DIR / "vontology_tools.json"


def _build_tool_cache_payload(
    tools: list[dict[str, Any]],
    config: MCPServerConfig,
) -> dict[str, Any]:
    return {
        "cached_at_utc": datetime.now(timezone.utc).isoformat(),
        "server_command": config.command,
        "server_args": config.args,
        "tools": tools,
    }


async def _list_and_cache_tools(
    client: MCPStdIOClient,
    config: MCPServerConfig,
    cache_path: Path,
    *,
    show_tools: bool,
    cache_tools: bool,
) -> None:
    if not (show_tools or cache_tools):
        return
    try:
        tools = await client.list_tools()
    except MCPToolClientError as exc:
        print(f"warning: could not list tools: {exc}", file=sys.stderr)
        return
    if show_tools:
        print(json.dumps(tools, indent=2, ensure_ascii=True), file=sys.stderr)
    if cache_tools:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = _build_tool_cache_payload(tools, config)
            cache_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=True),
                encoding="utf-8",
            )
            print(f"cached tool list to {cache_path}", file=sys.stderr)
        except OSError as exc:
            print(f"warning: could not write tool cache: {exc}", file=sys.stderr)


async def _run() -> int:
    parser = argparse.ArgumentParser(
        description="Query the Vontology MCP stdio server via MCP client."
    )
    parser.add_argument(
        "concept_id",
        help="Concept ID to fetch (e.g. #V#has_blob_backend).",
    )
    parser.add_argument(
        "--text-relations",
        choices=("false", "true", "snippets"),
        default="snippets",
        help="Include text relations arg1 (default: snippets).",
    )
    parser.add_argument(
        "--include-relations-arg1",
        action="store_true",
        help="Include structural relations where the concept is argument 1.",
    )
    parser.add_argument(
        "--include-relations-any-arg",
        action="store_true",
        help="Include structural relations where the concept appears in any position.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of relation entries to return.",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=None,
        help="Offset applied before collecting relation entries.",
    )
    parser.add_argument(
        "--show-tools",
        action="store_true",
        help="Print tool list to stderr before fetching.",
    )
    parser.add_argument(
        "--cache-tools",
        dest="cache_tools",
        action="store_true",
        help="Cache the tool list before fetching (default).",
    )
    parser.add_argument(
        "--no-cache-tools",
        dest="cache_tools",
        action="store_false",
        help="Disable tool list caching.",
    )
    parser.add_argument(
        "--tools-cache-path",
        type=Path,
        default=DEFAULT_TOOLS_CACHE,
        help="Path to write the tool list cache.",
    )
    parser.set_defaults(cache_tools=True)
    args = parser.parse_args()

    config = MCPServerConfig(
        command="pdm",
        args=["run", "python", "src/backend/mcp_server/mcp_stdio_server.py"],
        log_tag="[vontology_mcp]",
    )
    client = MCPStdIOClient(config)
    cache_path = Path(args.tools_cache_path)
    await _list_and_cache_tools(
        client,
        config,
        cache_path,
        show_tools=bool(args.show_tools),
        cache_tools=bool(args.cache_tools),
    )

    include_text_relations: bool | str
    if args.text_relations == "true":
        include_text_relations = True
    elif args.text_relations == "false":
        include_text_relations = False
    else:
        include_text_relations = "snippets"

    payload = {
        "concept_id": args.concept_id,
        "include_relations_arg1": bool(args.include_relations_arg1),
        "include_relations_any_arg": bool(args.include_relations_any_arg),
        "include_text_relations_arg1": include_text_relations,
    }
    if args.limit is not None:
        payload["limit"] = int(args.limit)
    if args.offset is not None:
        payload["offset"] = int(args.offset)

    result = await client.call_tool("fetch_concept", payload)
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
