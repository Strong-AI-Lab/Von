#!/usr/bin/env python3
"""Invoke a Von internal MCP tool from the repo root.

This is the canonical shell-side path for quick Jira/Vontology/MCP diagnostics
when the in-session MCP connector surface is unavailable or incomplete.

Examples:
    pdm run python utilities/invoke_internal_mcp_tool.py jira_get_auth_config
    pdm run python utilities/invoke_internal_mcp_tool.py jira_get_myself
    pdm run python utilities/invoke_internal_mcp_tool.py jira_get_issue --payload-json "{\"issue_key\":\"JVNAUTOSCI-1989\"}"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        payload_text = Path(args.payload_file).read_text(encoding="utf-8")
    elif args.payload_json:
        payload_text = args.payload_json
    else:
        return {}

    payload = json.loads(payload_text)
    if not isinstance(payload, dict):
        raise ValueError("payload must decode to a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Invoke a Von internal MCP tool via InternalMCPGateway.",
    )
    parser.add_argument("tool_name", help="Internal MCP tool name, e.g. jira_get_myself")
    parser.add_argument(
        "--payload-json",
        help="Inline JSON object payload to pass to the tool.",
    )
    parser.add_argument(
        "--payload-file",
        help="Path to a JSON file containing the payload object.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON instead of pretty-printed output.",
    )
    args = parser.parse_args(argv)

    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )

    os.environ.setdefault("VON_INTERNAL_MCP_ENABLE", "1")

    payload = _load_payload(args)
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    result = gateway.invoke(args.tool_name, payload).payload

    if args.compact:
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), default=str))
    else:
        print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
