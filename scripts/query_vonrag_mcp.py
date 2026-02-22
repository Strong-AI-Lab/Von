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
DEFAULT_TOOLS_CACHE = TOOLS_CACHE_DIR / "vonrag_tools.json"


def _resolve_python_command() -> str:
    venv_python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


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


def _load_json(text: str, label: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {exc}") from exc


def _read_payload_from_file(path: Path) -> Any:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read payload file {path}: {exc}") from exc
    return _load_json(content, str(path))


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
        description="Query the VonRAG MCP stdio server via MCP client."
    )
    parser.add_argument(
        "--tool",
        help="Tool name to call (e.g. rag_list_indexed).",
    )
    parser.add_argument(
        "--payload",
        help="JSON payload to pass to the tool.",
    )
    parser.add_argument(
        "--payload-file",
        type=Path,
        help="Path to a JSON file for tool arguments.",
    )
    parser.add_argument(
        "--show-tools",
        action="store_true",
        help="Print tool list to stderr before calling the tool.",
    )
    parser.add_argument(
        "--cache-tools",
        dest="cache_tools",
        action="store_true",
        help="Cache the tool list before calling the tool (default).",
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

    if args.payload and args.payload_file:
        print("error: use only one of --payload or --payload-file.", file=sys.stderr)
        return 2

    config = MCPServerConfig(
        command=_resolve_python_command(),
        args=[str(REPO_ROOT / "src/backend/mcp_server/rag_mcp_stdio_server.py")],
        log_tag="[vonrag_mcp]",
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

    if not args.tool:
        if args.show_tools or args.cache_tools:
            return 0
        print("error: --tool is required unless you are listing tools.", file=sys.stderr)
        return 2

    payload: Any = {}
    if args.payload_file:
        try:
            payload = _read_payload_from_file(Path(args.payload_file))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    elif args.payload:
        try:
            payload = _load_json(args.payload, "--payload")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        result = await client.call_tool(args.tool, payload)
    except MCPToolClientError as exc:
        print(f"error: tool call failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
