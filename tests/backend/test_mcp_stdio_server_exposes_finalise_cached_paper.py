import json
from pathlib import Path


def test_mcp_stdio_server_has_finalise_cached_paper_handler():
    from src.backend.mcp_server import mcp_stdio_server

    assert "finalise_cached_paper" in mcp_stdio_server._TOOL_HANDLERS
    assert (
        "materialise_scholarly_representation_for_file_copy"
        in mcp_stdio_server._TOOL_HANDLERS
    )


def test_vontology_mcp_manifest_includes_finalise_cached_paper():
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
    assert "finalise_cached_paper" in names
    assert "materialise_scholarly_representation_for_file_copy" in names
