import json
from pathlib import Path


JIRA_TOOL_NAMES = {
    "jira_search",
    "jira_get_issue",
    "jira_get_bulk_operation_progress",
    "jira_get_transitions",
    "jira_add_comment",
    "jira_add_attachment",
    "jira_transition",
    "jira_create_issue",
    "jira_move_issue",
    "jira_update_issue",
    "jira_link_issue",
    "jira_delete_issue_link",
    "jira_get_myself",
    "jira_get_auth_config",
    "jira_get_project_issue_types",
}


def test_mcp_stdio_server_has_jira_tool_handlers():
    from src.backend.mcp_server import mcp_stdio_server

    for tool_name in JIRA_TOOL_NAMES:
        assert tool_name in mcp_stdio_server._TOOL_HANDLERS


def test_vontology_mcp_manifest_includes_jira_tools():
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
    missing = JIRA_TOOL_NAMES - names
    assert not missing, f"Manifest missing Jira tools: {sorted(missing)}"

    jira_get_issue = next(
        tool
        for tool in tools
        if isinstance(tool, dict) and tool.get("name") == "jira_get_issue"
    )
    properties = jira_get_issue.get("inputSchema", {}).get("properties", {})
    assert "expand" in properties

    jira_get_project_issue_types = next(
        tool
        for tool in tools
        if isinstance(tool, dict)
        and tool.get("name") == "jira_get_project_issue_types"
    )
    project_issue_types_properties = (
        jira_get_project_issue_types.get("inputSchema", {}).get("properties", {})
    )
    assert "project_key" in project_issue_types_properties
