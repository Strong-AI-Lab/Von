#!/usr/bin/env python3
"""Invoke a Von internal MCP tool from the repo root.

This is the canonical shell-side path for quick Jira/Vontology/MCP diagnostics
when the in-session MCP connector surface is unavailable or incomplete.

Examples:
    python utilities/invoke_internal_mcp_tool.py jira_get_auth_config
    python utilities/invoke_internal_mcp_tool.py jira_get_myself
    python utilities/invoke_internal_mcp_tool.py jira_get_issue --payload-json "{\"issue_key\":\"JVNAUTOSCI-1989\"}"

Plain ``python`` calls automatically delegate through ``pdm run python`` when
needed, so quick probes do not accidentally use a system interpreter that lacks
repo dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


class _CompletedProcessLike(Protocol):
    returncode: int


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _is_project_python(
    *,
    environ: Mapping[str, str] | None = None,
    prefix: str | None = None,
) -> bool:
    """Return whether the current interpreter is already project-managed."""

    env = environ if environ is not None else os.environ

    pdm_project_root = env.get("PDM_PROJECT_ROOT")
    if pdm_project_root and Path(pdm_project_root).resolve() == PROJECT_ROOT:
        return True

    virtual_env = env.get("VIRTUAL_ENV")
    if virtual_env and _is_relative_to(Path(virtual_env), PROJECT_ROOT):
        return True

    interpreter_prefix = Path(prefix or sys.prefix)
    return _is_relative_to(interpreter_prefix, PROJECT_ROOT)


def _build_pdm_delegate_args(argv: Sequence[str]) -> list[str]:
    return [
        "pdm",
        "run",
        "python",
        str(Path(__file__).resolve()),
        *list(argv),
    ]


def _maybe_delegate_via_pdm(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    prefix: str | None = None,
    which: Callable[[str], str | None] | None = None,
    runner: Callable[..., _CompletedProcessLike] | None = None,
) -> int | None:
    """Run through PDM when launched from a non-project interpreter."""

    env = environ if environ is not None else os.environ
    if _truthy(env.get("VON_INTERNAL_MCP_TOOL_NO_PDM_DELEGATE")):
        return None
    if _is_project_python(environ=env, prefix=prefix):
        return None

    resolve = which if which is not None else shutil.which
    pdm_executable = resolve("pdm")
    if not pdm_executable:
        return None

    command = _build_pdm_delegate_args(argv)
    command[0] = pdm_executable
    run = runner if runner is not None else subprocess.run
    completed = run(command, cwd=PROJECT_ROOT)
    return int(completed.returncode)


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
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    pdm_returncode = _maybe_delegate_via_pdm(effective_argv)
    if pdm_returncode is not None:
        return pdm_returncode

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
    args = parser.parse_args(effective_argv)

    try:
        from src.backend.integrations.internal_mcp import (
            InternalMCPGateway,
            InternalMCPTransport,
            build_default_catalogue,
        )
    except ModuleNotFoundError as exc:
        print(
            "Could not import Von's internal MCP dependencies. Run from the repo "
            "root with `pdm run python utilities/invoke_internal_mcp_tool.py ...`, "
            "or install PDM so this helper can delegate automatically. "
            f"Missing module: {exc.name}",
            file=sys.stderr,
        )
        return 2

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
