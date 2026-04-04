import json
import os
import subprocess
import sys
from pathlib import Path
import asyncio
from typing import Any, cast

from src.backend.integrations.internal_mcp.tool_contract_registry import (
    SURFACE_MANIFEST,
    SURFACE_VONTOLOGY_STDIO,
    get_surface_tool_payloads,
)
from src.backend.mcp_server.mcp_stdio_server import list_tools

PROJECT_ROOT = Path(__file__).parent.parent.parent


def _normalise_surface_payload(payload: dict) -> dict:
    return {
        "name": payload.get("name"),
        "description": payload.get("description"),
        "inputSchema": payload.get("inputSchema"),
    }


def test_vontology_stdio_surface_matches_canonical_registry() -> None:
    """Vontology stdio list_tools must be generated from canonical contracts."""
    async def _invoke_list_tools() -> list[Any]:
        list_tools_fn = cast(Any, list_tools)
        result = await list_tools_fn()
        return cast(list[Any], result)

    code_tools = asyncio.run(_invoke_list_tools())
    code_payload = {
        tool.name: {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.inputSchema,
        }
        for tool in code_tools
    }

    canonical_payload = {
        item["name"]: _normalise_surface_payload(item)
        for item in get_surface_tool_payloads(SURFACE_VONTOLOGY_STDIO)
    }

    assert set(code_payload.keys()) == set(canonical_payload.keys())
    for name in canonical_payload:
        assert _normalise_surface_payload(code_payload[name]) == _normalise_surface_payload(
            canonical_payload[name]
        ), f"Vontology stdio tool contract drift for '{name}'"


def test_manifest_surface_matches_canonical_registry() -> None:
    """Manifest must exactly match canonical contracts for the manifest surface."""
    manifest_path = (
        PROJECT_ROOT / "src" / "backend" / "mcp_server" / "vontology_mcp.json"
    )
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)

    manifest_tools = manifest_data.get("tools") or []
    manifest_payload = {
        item.get("name"): _normalise_surface_payload(item)
        for item in manifest_tools
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    canonical_payload = {
        item["name"]: _normalise_surface_payload(item)
        for item in get_surface_tool_payloads(SURFACE_MANIFEST)
    }

    assert set(manifest_payload.keys()) == set(canonical_payload.keys())
    for name in canonical_payload:
        assert _normalise_surface_payload(manifest_payload[name]) == _normalise_surface_payload(
            canonical_payload[name]
        ), f"Manifest tool contract drift for '{name}'"


def test_manifest_regeneration_script_runs_from_repo_root_without_pythonpath() -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    manifest_path = (
        PROJECT_ROOT / "src" / "backend" / "mcp_server" / "vontology_mcp.json"
    )
    original_manifest = manifest_path.read_text(encoding="utf-8")

    try:
        result = subprocess.run(
            [sys.executable, "scripts/regenerate_vontology_mcp_manifest.py"],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "Regenerated" in result.stdout
    finally:
        regenerated_manifest = manifest_path.read_text(encoding="utf-8")
        if regenerated_manifest != original_manifest:
            manifest_path.write_text(original_manifest, encoding="utf-8")
