import pytest
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    ToolCallParsingError,
)


class _DummyGateway:
    enabled = True

    def describe_methods(self):
        return {"test": {"description": "dummy"}}


def test_extract_tool_calls_accepts_single_tool_call():
    text = '{"action": "call_tool", "tool": "test", "payload": {}}'
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls(text)
    assert calls == [{"action": "call_tool", "tool": "test", "payload": {}}]


def test_extract_tool_calls_accepts_json_array_batch():
    text = (
        '[{"action":"call_tool","tool":"test","payload":{}},'
        '{"action":"call_tool","tool":"test","payload":{"a":1}}]'
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls("".join(text))
    assert calls is not None
    assert len(calls) == 2
    assert calls[0]["tool"] == "test"
    assert calls[1]["payload"]["a"] == 1


def test_extract_tool_calls_accepts_fenced_json_array_batch():
    text = (
        "Here is the tool batch:\n"
        "```json\n"
        '[{"action":"call_tool","tool":"test","payload":{}},'
        '{"action":"call_tool","tool":"test","payload":{}}]\n'
        "```"
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls(text)
    assert calls is not None
    assert len(calls) == 2


def test_extract_tool_calls_accepts_missing_action_when_tool_is_known_in_batch():
    text = '[{"tool": "test", "payload": {}}, {"tool": "test", "payload": {"x": 2}}]'
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls(text)
    assert calls is not None
    assert calls[0]["action"] == "call_tool"
    assert calls[1]["action"] == "call_tool"
    assert calls[1]["payload"]["x"] == 2


def test_extract_tool_calls_rejects_concatenated_json_objects():
    text = (
        '{"action": "call_tool", "tool": "a", "payload": {}}\n'
        '{"action": "call_tool", "tool": "b", "payload": {}}'
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    with pytest.raises(ToolCallParsingError):
        orchestrator._extract_tool_calls(text)


def test_extract_json_blob_pure_json():
    text = '{"action": "call_tool", "tool": "test", "payload": {}}'
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    result = orchestrator._extract_json_blob(text)
    assert result == {"action": "call_tool", "tool": "test", "payload": {}}


def test_extract_json_blob_code_block():
    text = 'Here is the tool call:\n```json\n{"action": "call_tool", "tool": "test", "payload": {}}\n```'
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    assert orchestrator._extract_json_blob(text) == {
        "action": "call_tool",
        "tool": "test",
        "payload": {},
    }


def test_extract_json_blob_embedded():
    text = 'I will call the tool now.\n{"action": "call_tool", "tool": "test", "payload": {}}\nThis should work.'
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    assert orchestrator._extract_json_blob(text) is None


def test_extract_json_blob_embedded_with_newlines():
    text = 'Explanation...\n{\n  "action": "call_tool",\n  "tool": "test",\n  "payload": {}\n}\nEnd.'
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    assert orchestrator._extract_json_blob(text) is None


def test_extract_json_blob_invalid():
    text = "Just some text."
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    result = orchestrator._extract_json_blob(text)
    assert result is None


def test_extract_json_blob_malformed_json():
    text = '{"action": "call_tool", ...'
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    with pytest.raises(ToolCallParsingError):
        orchestrator._extract_json_blob(text)


def test_extract_json_blob_accepts_trailing_characters():
    text = '{"action": "call_tool", "tool": "test", "payload": {}}x'
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    result = orchestrator._extract_json_blob(text)
    assert result == {"action": "call_tool", "tool": "test", "payload": {}}


def test_extract_json_blob_accepts_trailing_prose_after_tool_call():
    text = (
        '{"action": "call_tool", "tool": "test", "payload": {}}\n'
        "I will now execute this tool to gather the facts before answering."
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    result = orchestrator._extract_json_blob(text)
    assert result == {"action": "call_tool", "tool": "test", "payload": {}}


def test_extract_json_blob_rejects_multiple_json_objects():
    text = (
        '{"action": "call_tool", "tool": "a", "payload": {}}\n'
        '{"action": "call_tool", "tool": "b", "payload": {}}'
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    with pytest.raises(ToolCallParsingError):
        orchestrator._extract_json_blob(text)


def test_extract_json_blob_accepts_missing_action_when_tool_is_known():
    text = '{"tool": "test", "payload": {"a": 1}}'
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    result = orchestrator._extract_json_blob(text)
    assert result == {"action": "call_tool", "tool": "test", "payload": {"a": 1}}


def test_extract_json_blob_ignores_missing_action_when_tool_is_unknown():
    text = '{"tool": "not_a_tool", "payload": {"a": 1}}'
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    assert orchestrator._extract_json_blob(text) is None


def test_looks_like_missing_tool_call_detects_promise_to_search():
    """Test that 'I'm going to search...' without actual tool call is detected."""
    text = "I'm going to search the web for authoritative information about you."
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    assert orchestrator._looks_like_missing_tool_call(text)


def test_looks_like_missing_tool_call_detects_promise_to_execute_tools():
    """Test that 'I'll execute the tools' without actual tool call is detected."""
    text = "I'll fix that now and actually execute the tools."
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    assert orchestrator._looks_like_missing_tool_call(text)


def test_looks_like_missing_tool_call_detects_promise_to_fetch():
    """Test that 'Let me fetch...' without actual tool call is detected."""
    text = "Let me fetch the concept details now."
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    assert orchestrator._looks_like_missing_tool_call(text)


def test_looks_like_missing_tool_call_ignores_short_explanatory_text():
    """Test that normal explanatory text without tool promises is not detected."""
    text = "I'll help you with that. What would you like to know?"
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    assert not orchestrator._looks_like_missing_tool_call(text)


def test_looks_like_missing_tool_call_ignores_long_prose():
    """Test that long prose responses (>500 chars) don't trigger false positives."""
    text = (
        "I'm going to explain this carefully. " * 20  # Makes it > 500 chars
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    assert not orchestrator._looks_like_missing_tool_call(text)
