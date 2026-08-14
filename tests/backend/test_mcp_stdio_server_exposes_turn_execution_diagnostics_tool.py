import asyncio
import json
from pathlib import Path


def test_mcp_stdio_server_has_turn_execution_diagnostics_handler() -> None:
    from src.backend.mcp_server import mcp_stdio_server

    assert "turn_execution_get_diagnostics" in mcp_stdio_server._TOOL_HANDLERS
    assert "failure_case_intake_collect" in mcp_stdio_server._TOOL_HANDLERS
    assert "failure_case_reference_resolve" in mcp_stdio_server._TOOL_HANDLERS
    assert "chat_history_get_segments" in mcp_stdio_server._TOOL_HANDLERS
    assert "chat_history_get_debug_entry" in mcp_stdio_server._TOOL_HANDLERS
    assert "conversation_telemetry_get_locator" in mcp_stdio_server._TOOL_HANDLERS
    assert "turn_execution_get_live_progress" in mcp_stdio_server._TOOL_HANDLERS
    assert "workflow_list_use_episodes" in mcp_stdio_server._TOOL_HANDLERS
    assert "mongo_query_diagnostics_report" in mcp_stdio_server._TOOL_HANDLERS


def test_mongo_query_diagnostics_stdio_routes_the_runtime_binding(monkeypatch) -> None:
    from src.backend.mcp_server import mcp_stdio_server

    monkeypatch.setattr(
        mcp_stdio_server,
        "_mongo_query_diagnostics_report",
        lambda **_kwargs: {"success": True, "source": "stdio_runtime_binding"},
    )

    result = asyncio.run(
        mcp_stdio_server.call_tool(
            "mongo_query_diagnostics_report",
            {"allow_operator_diagnostics": True},
        )
    )
    payload = json.loads(result[0].text)

    assert payload == {"success": True, "source": "stdio_runtime_binding"}


def test_vontology_mcp_manifest_includes_turn_execution_diagnostics_tool() -> None:
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
    assert "turn_execution_get_diagnostics" in names
    assert "failure_case_intake_collect" in names
    assert "failure_case_reference_resolve" in names
    assert "chat_history_get_segments" in names
    assert "chat_history_get_debug_entry" in names
    assert "conversation_telemetry_get_locator" in names
    assert "turn_execution_get_live_progress" in names
    assert "workflow_list_use_episodes" in names
    assert "mongo_query_diagnostics_report" in names
    mongo_tool = next(
        tool for tool in tools if tool.get("name") == "mongo_query_diagnostics_report"
    )
    assert mongo_tool["inputSchema"]["properties"]["source"]["enum"] == [
        "combined",
        "in_process",
        "profiler",
        "query_stats",
    ]
