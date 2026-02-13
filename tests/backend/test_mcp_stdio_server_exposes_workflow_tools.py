import json
from pathlib import Path


WORKFLOW_TOOL_NAMES = {
    "workflow_list_definitions",
    "workflow_bind_event",
    "workflow_list_event_bindings",
    "workflow_mcp_health_check",
    "workflow_create_instance",
    "workflow_list_instances",
    "workflow_get_instance",
    "workflow_cancel_instance",
    "workflow_retry_instance",
    "workflow_create_schedule",
    "workflow_list_schedules",
    "workflow_get_schedule",
    "workflow_set_schedule_enabled",
    "workflow_delete_schedule",
    "workflow_trigger_schedule",
}


def test_mcp_stdio_server_has_workflow_tool_handlers():
    from src.backend.mcp_server import mcp_stdio_server

    for tool_name in WORKFLOW_TOOL_NAMES:
        assert tool_name in mcp_stdio_server._TOOL_HANDLERS


def test_vontology_mcp_manifest_includes_workflow_tools():
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
    missing = WORKFLOW_TOOL_NAMES - names
    assert not missing, f"Manifest missing workflow tools: {sorted(missing)}"
