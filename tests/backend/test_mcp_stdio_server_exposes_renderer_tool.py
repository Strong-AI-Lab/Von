import json
from pathlib import Path


def test_mcp_stdio_server_has_renderer_tool_handler() -> None:
    from src.backend.mcp_server import mcp_stdio_server

    assert "renderer_resolve_applicability" in mcp_stdio_server._TOOL_HANDLERS
    assert "upsert_renderer_profile" in mcp_stdio_server._TOOL_HANDLERS


def test_vontology_mcp_manifest_includes_renderer_tool() -> None:
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
    assert "renderer_resolve_applicability" in names
    assert "upsert_renderer_profile" in names
