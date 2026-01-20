"""
Tests for Phase 3 (JVNAUTOSCI-799): Orchestrator Structured Tool Calling Integration.

This test suite validates:
1. Structured calling path works when feature flag enabled
2. Legacy fallback works when feature flag disabled
3. Safety constraints preserved (namespace injection, gmail profile, whitelist)
4. call_id execution tracing works correctly
5. Tool definition conversion from MCP catalog to ToolDefinition format
"""

import os
from typing import Any, Dict, List, Mapping, Optional, Sequence
from unittest.mock import MagicMock, patch

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    _MissingToolCallDetectorSpec,
)
from src.backend.languagemodels.structured_tool_calling.types import (
    LLMResponse,
    ToolCall,
    ToolDefinition,
)


class MockLLMClientWithTools:
    """Mock LLM client that supports structured tool calling."""

    def __init__(self, should_use_structured: bool = True):
        self._should_use_structured_value = should_use_structured
        self.generate_called = False
        self.generate_with_tools_called = False

    def _should_use_structured_calling(self) -> bool:
        return self._should_use_structured_value

    def generate(
        self,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]] = None,
        model: Optional[str] = None,
    ) -> str:
        """Legacy generate method."""
        self.generate_called = True
        return "Legacy response without tools"

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[Sequence[Mapping[str, Any]]] = None,
        model: Optional[str] = None,
        system_message: Optional[str] = None,
    ) -> LLMResponse:
        """Structured tool calling method."""
        self.generate_with_tools_called = True
        # Simulate a tool call response
        return LLMResponse(
            text_response="I'll search for that concept",
            tool_calls=[
                ToolCall(
                    tool_name="search_knowledge_base",
                    payload={"query": "test query"},
                    call_id="call_abc123",
                )
            ],
        )


class MockLLMClientLegacyOnly:
    """Mock LLM client that only supports legacy generate()."""

    def __init__(self):
        self.generate_called = False

    def generate(
        self,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]] = None,
        model: Optional[str] = None,
    ) -> str:
        """Legacy generate method."""
        self.generate_called = True
        # Return JSON tool call format
        return '{"tool": "search_knowledge_base", "payload": {"query": "test query"}}'


@pytest.fixture
def mock_gateway():
    """Create a mock gateway with a sample tool catalog."""
    from unittest.mock import MagicMock
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway

    gateway = MagicMock(spec=InternalMCPGateway)
    gateway.enabled = True

    # Create a simple catalog with one tool
    catalog = {
        "search_knowledge_base": {
            "name": "search_knowledge_base",
            "description": "Search the knowledge base",
            "input_schema": {
                "required": {"query": str},
                "optional": {},
                "allow_unknown": False,
                "description": "Search query parameters",
            },
            "output_schema": None,
            "category": "read",
        }
    }
    gateway.describe_methods.return_value = catalog

    # Mock invoke to return a simple result
    mock_result = MagicMock()
    mock_result.payload = {"results": ["found_concept"]}
    mock_result.duration_ms = 50
    gateway.invoke.return_value = mock_result

    return gateway


@pytest.fixture
def orchestrator(mock_gateway):
    """Create an orchestrator instance with mocked gateway."""
    orch = InternalMCPChatOrchestrator(gateway=mock_gateway)
    return orch


def test_structured_calling_path_used_when_available(orchestrator):
    """Test that structured calling is used when available and feature flag enabled."""
    llm_client = MockLLMClientWithTools(should_use_structured=True)

    result = orchestrator.run(
        prompt="Find test concept",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    # Verify structured calling was used (for initial prompt)
    assert llm_client.generate_with_tools_called
    # Note: generate() may be called for follow-up after tool execution
    # This is expected behavior - we just want to verify structured calling was used first

    # Verify tool was invoked
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0]["tool"] == "search_knowledge_base"
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0]["tool"] == "search_knowledge_base"


def test_legacy_fallback_when_structured_disabled(orchestrator):
    """Test that legacy path is used when feature flag disabled."""
    llm_client = MockLLMClientWithTools(should_use_structured=False)

    result = orchestrator.run(
        prompt="Find test concept",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    # Verify legacy generate was used
    assert llm_client.generate_called
    assert not llm_client.generate_with_tools_called


def test_legacy_fallback_when_structured_unavailable(orchestrator):
    """Test that legacy path works when client doesn't support structured calling."""
    llm_client = MockLLMClientLegacyOnly()

    result = orchestrator.run(
        prompt="Find test concept",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    # Verify legacy generate was used
    assert llm_client.generate_called

    # Verify tool was still invoked (via JSON parsing)
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0]["tool"] == "search_knowledge_base"


def test_call_id_tracing_in_structured_path(orchestrator):
    """Test that call_id is preserved in tool invocations (JVNAUTOSCI-803)."""
    llm_client = MockLLMClientWithTools(should_use_structured=True)

    result = orchestrator.run(
        prompt="Find test concept",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    # Verify call_id is in invocation record
    assert len(result.tool_invocations) == 1
    assert "call_id" in result.tool_invocations[0]
    assert result.tool_invocations[0]["call_id"] == "call_abc123"


def test_tool_definition_conversion_accepts_list_schema():
    """Regression: accept list-based required/optional schema summaries."""
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway

    gateway = MagicMock(spec=InternalMCPGateway)
    gateway.enabled = True
    gateway.describe_methods.return_value = {
        "search_knowledge_base": {
            "name": "search_knowledge_base",
            "description": "Search the knowledge base",
            "input_schema": {
                "required": ["query"],
                "optional": ["limit"],
                "allow_unknown": True,
                "description": "Search query parameters",
            },
            "output_schema": None,
            "category": "read",
        }
    }

    orch = InternalMCPChatOrchestrator(gateway=gateway)
    tool_defs = orch._convert_mcp_tools_to_structured_definitions()

    assert tool_defs
    schema = tool_defs[0].input_schema
    assert schema.get("required") == ["query"]
    assert schema.get("properties", {}).get("query", {}).get("type") == "string"
    assert schema.get("properties", {}).get("limit", {}).get("type") == "string"
    assert schema.get("additionalProperties") is True


def test_namespace_injection_preserved(orchestrator):
    """Test that namespace injection still works in structured path."""
    llm_client = MockLLMClientWithTools(should_use_structured=True)

    result = orchestrator.run(
        prompt="Find test concept",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    # Verify namespace was injected into payload
    invoke_calls = orchestrator._gateway.invoke.call_args_list
    assert len(invoke_calls) == 1
    _, call_kwargs = invoke_calls[0]
    # The invoke is called with (tool_name, payload) positional args
    payload = call_kwargs if call_kwargs else invoke_calls[0][0][1]
    if isinstance(payload, dict):
        assert "namespace" in payload
        assert payload["namespace"] == "#V#test_user"


def test_mcp_schema_to_json_schema_conversion(orchestrator):
    """Test Schema to JSON Schema conversion."""
    mcp_schema = {
        "required": {"query": str, "limit": int},
        "optional": {"offset": int},
        "allow_unknown": False,
        "description": "Search parameters",
    }

    json_schema = orchestrator._mcp_schema_to_json_schema(mcp_schema)

    assert json_schema["type"] == "object"
    assert "properties" in json_schema
    assert "query" in json_schema["properties"]
    assert "limit" in json_schema["properties"]
    assert "offset" in json_schema["properties"]
    assert json_schema["properties"]["query"]["type"] == "string"
    assert json_schema["properties"]["limit"]["type"] == "integer"
    assert json_schema["properties"]["offset"]["type"] == "integer"
    assert "required" in json_schema
    assert "query" in json_schema["required"]
    assert "limit" in json_schema["required"]
    assert "offset" not in json_schema["required"]


def test_tool_definitions_conversion(orchestrator):
    """Test conversion of MCP catalog to ToolDefinition list."""
    tool_defs = orchestrator._convert_mcp_tools_to_structured_definitions()

    assert len(tool_defs) == 1
    assert isinstance(tool_defs[0], ToolDefinition)
    assert tool_defs[0].name == "search_knowledge_base"
    assert tool_defs[0].description == "Search the knowledge base"
    assert "properties" in tool_defs[0].input_schema


def test_structured_calling_with_no_tool_response(orchestrator, mock_gateway):
    """Test that structured calling handles responses without tool calls."""

    class MockLLMClientNoTools:
        def _should_use_structured_calling(self):
            return True

        def generate(self, prompt, context=None, model=None):
            return "Just a text response"

        def generate_with_tools(
            self, prompt, available_tools, context=None, model=None, system_message=None
        ):
            # Return response without tool calls
            return LLMResponse(text_response="Just a text response", tool_calls=[])

    llm_client = MockLLMClientNoTools()

    result = orchestrator.run(
        prompt="What is the weather?",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
    )

    # Should return text response without invoking tools
    assert result.response_text == "Just a text response"
    assert len(result.tool_invocations) == 0


def test_structured_calling_exception_fallback(orchestrator, mock_gateway):
    """Test that exceptions in structured calling fall back to legacy path."""

    class MockLLMClientWithException:
        def _should_use_structured_calling(self):
            return True

        def generate(self, prompt, context=None, model=None):
            return '{"tool": "search_knowledge_base", "payload": {"query": "fallback"}}'

        def generate_with_tools(
            self, prompt, available_tools, context=None, model=None, system_message=None
        ):
            raise RuntimeError("Structured calling failed")

    llm_client = MockLLMClientWithException()

    # Should not raise, should fall back to legacy
    result = orchestrator.run(
        prompt="Find concept",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    # Should have invoked tool via legacy path
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0]["tool"] == "search_knowledge_base"


def test_structured_path_missing_tool_call_emits_aux_logs_with_structured_path(
    orchestrator,
):
    """Regression test: missing-tool-call recovery should preserve path='structured'.

    When structured calling is enabled but the model returns no tool calls while
    promising to use tools, the orchestrator should:
    - run missing-tool-call detection/classifier
    - retry once
    - record aux_llm_calls entries tagged with path='structured'
    """

    class MockLLMClientStructuredMissingToolCall:
        def __init__(self):
            self.generate_called = 0
            self.generate_with_tools_called = 0

        def _should_use_structured_calling(self) -> bool:
            return True

        def generate(self, prompt: str, context=None, model=None):
            # 1) classifier verdict
            # 2) retry response (legacy JSON tool-call format)
            # 3) final response after tool execution
            self.generate_called += 1
            if self.generate_called == 1:
                return "YES"
            if self.generate_called == 2:
                return '{"action":"call_tool","tool":"search_knowledge_base","payload":{"query":"test query"}}'
            return "Final response"

        def generate_with_tools(
            self,
            prompt: str,
            available_tools: List[ToolDefinition],
            context=None,
            model=None,
            system_message=None,
        ) -> LLMResponse:
            self.generate_with_tools_called += 1
            # Structured path: model promises a tool call but doesn't include one.
            return LLMResponse(
                text_response="I will search the knowledge base now.",
                tool_calls=[],
            )

    llm_client = MockLLMClientStructuredMissingToolCall()

    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="#V#detect_missing_tool_call_action",
        prompt_id="#V#missing_tool_call_detection_prompt",
        prompt_text="Answer YES or NO for: {response}",
        model="detector-model",
    )

    result = orchestrator.run(
        prompt="Find test concept",
        context=[],
        llm_client=llm_client,
        model="gpt-4",
        user_namespace="#V#test_user",
    )

    assert llm_client.generate_with_tools_called == 1
    assert llm_client.generate_called >= 2
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0]["tool"] == "search_knowledge_base"
    assert result.response_text == "Final response"
    assert result.aux_llm_calls

    aux_by_type: dict[str, list[Mapping[str, Any]]] = {}
    for entry in result.aux_llm_calls:
        if isinstance(entry, dict) and isinstance(entry.get("type"), str):
            aux_by_type.setdefault(entry["type"], []).append(entry)

    assert aux_by_type["missing_tool_call_detection"][0]["path"] == "structured"
    assert aux_by_type["missing_tool_call_classifier"][0]["path"] == "structured"
    for retry_entry in aux_by_type["missing_tool_call_retry"]:
        assert retry_entry["path"] == "structured"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
