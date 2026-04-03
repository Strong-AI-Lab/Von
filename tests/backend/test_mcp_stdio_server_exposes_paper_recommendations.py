import json
from pathlib import Path


def test_mcp_stdio_server_has_build_paper_recommendations_handler():
    from src.backend.mcp_server import mcp_stdio_server

    assert "build_paper_recommendations" in mcp_stdio_server._TOOL_HANDLERS


def test_vontology_mcp_manifest_includes_build_paper_recommendations():
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
    assert "build_paper_recommendations" in names
