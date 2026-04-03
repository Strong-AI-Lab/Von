import json
from pathlib import Path


SKILL_CATALOGUE_TOOL_NAMES = {
    "skill_catalogue_list",
    "skill_catalogue_sync",
}


def test_mcp_stdio_server_has_skill_catalogue_handlers():
    from src.backend.mcp_server import mcp_stdio_server

    for tool_name in SKILL_CATALOGUE_TOOL_NAMES:
        assert tool_name in mcp_stdio_server._TOOL_HANDLERS


def test_vontology_mcp_manifest_includes_skill_catalogue_tools():
    manifest = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "backend"
        / "mcp_server"
        / "vontology_mcp.json"
    )
    data = json.loads(manifest.read_text(encoding="utf-8"))
    tools = data.get("tools") or []
    names = {t.get("name") for t in tools if isinstance(t, dict)}
    missing = SKILL_CATALOGUE_TOOL_NAMES - names
    assert not missing, f"Manifest missing skill catalogue tools: {sorted(missing)}"
