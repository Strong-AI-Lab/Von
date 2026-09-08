"""An explicit workflow's tool vocabulary survives changing family hints."""

from unittest.mock import MagicMock

from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)


def test_authored_mail_tools_survive_task_followup_without_becoming_required(
    monkeypatch,
):
    gateway = MagicMock(spec=InternalMCPGateway)
    gateway.enabled = True
    catalogue = {
        name: {
            "description": name,
            "category": "read",
            "input_schema": {"required": {}, "optional": {}},
        }
        for name in (
            "task_get",
            "gmail_get_message",
            "gmail_list_messages",
            "fetch_concept",
        )
    }
    gateway.describe_methods.return_value = catalogue
    orch = InternalMCPChatOrchestrator(gateway=gateway)
    monkeypatch.setattr(
        orch, "_collect_structured_tool_family_hints", lambda **k: ("vontology",)
    )
    definitions = orch._convert_mcp_tools_to_structured_definitions(
        method_catalogue=catalogue
    )
    arguments = dict(
        prompt="Continue after reading the task",
        context=[],
        stage="tool_follow_up",
        workflow_action_id="llm.action",
        provider="openai",
        tool_definitions=definitions,
        method_catalogue=catalogue,
        required_prompt_tools=[],
    )
    baseline = orch._resolve_structured_tool_candidates(**arguments)
    assert "gmail_get_message" not in baseline.candidate_tool_names
    authored = orch._resolve_structured_tool_candidates(
        **arguments, authored_tool_names=[*catalogue, "unavailable_tool"]
    )
    assert set(authored.candidate_tool_names) == set(catalogue)
    assert authored.required_tools == ()
    assert "unavailable_tool" not in authored.candidate_tool_names

    orch._structured_tool_candidate_cap_override = 2
    capped = orch._resolve_structured_tool_candidates(
        **arguments, authored_tool_names=list(catalogue)
    )
    assert len(capped.candidate_tool_names) == 2
    assert capped.truncation_applied
