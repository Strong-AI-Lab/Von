"""Tests for structured tool calling types."""

import pytest
from datetime import datetime

from src.backend.languagemodels.structured_tool_calling import (
    ToolCall,
    ToolDefinition,
    LLMResponse,
    ToolCallError,
)


class TestToolDefinition:
    """Tests for ToolDefinition validation."""

    def test_valid_tool_definition(self):
        """Test creating a valid ToolDefinition."""
        tool = ToolDefinition(
            name="get_concept",
            description="Retrieve concept details from Vontology",
            input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
        )

        assert tool.name == "get_concept"
        assert tool.description == "Retrieve concept details from Vontology"
        assert tool.input_schema["type"] == "object"
        assert tool.output_schema is None

    def test_tool_definition_with_output_schema(self):
        """Test ToolDefinition with output schema."""
        tool = ToolDefinition(
            name="list_concepts",
            description="List all concepts",
            input_schema={"type": "object"},
            output_schema={"type": "array", "items": {"type": "object"}},
        )

        assert tool.output_schema is not None
        assert tool.output_schema["type"] == "array"

    def test_invalid_tool_name(self):
        """Test that empty tool name raises ValueError."""
        with pytest.raises(ValueError):
            ToolDefinition(
                name="",
                description="Invalid",
                input_schema={"type": "object"},
            )

    def test_invalid_input_schema(self):
        """Test that invalid input_schema raises ValueError."""
        with pytest.raises(ValueError):
            ToolDefinition(
                name="test",
                description="Test tool",
                input_schema="not a dict",  # type: ignore[arg-type]
            )


class TestToolCall:
    """Tests for ToolCall."""

    def test_valid_tool_call(self):
        """Test creating a valid ToolCall."""
        call = ToolCall(
            tool_name="get_concept",
            payload={"id": "concept_123"},
        )

        assert call.tool_name == "get_concept"
        assert call.payload["id"] == "concept_123"
        assert call.call_id is not None
        assert isinstance(call.timestamp, datetime)

    def test_tool_call_with_explicit_call_id(self):
        """Test ToolCall with explicit call_id."""
        call = ToolCall(
            tool_name="test",
            payload={},
            call_id="custom-id-123",
        )

        assert call.call_id == "custom-id-123"

    def test_invalid_tool_name(self):
        """Test that empty tool_name raises ValueError."""
        with pytest.raises(ValueError):
            ToolCall(tool_name="", payload={})

    def test_invalid_payload(self):
        """Test that non-dict payload raises ValueError."""
        with pytest.raises(ValueError):
            ToolCall(tool_name="test", payload="not a dict")  # type: ignore[arg-type]


class TestLLMResponse:
    """Tests for LLMResponse."""

    def test_text_only_response(self):
        """Test LLMResponse with only text."""
        response = LLMResponse(text_response="Hello, world!")

        assert response.text_response == "Hello, world!"
        assert response.tool_calls == []
        assert not response.has_tool_calls()

    def test_tool_call_response(self):
        """Test LLMResponse with tool calls."""
        tool_call = ToolCall(tool_name="test_tool", payload={"arg": "value"})
        response = LLMResponse(
            text_response="I'll fetch that for you.",
            tool_calls=[tool_call],
        )

        assert response.text_response == "I'll fetch that for you."
        assert response.has_tool_calls()
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].tool_name == "test_tool"

    def test_response_with_usage(self):
        """Test LLMResponse with token usage."""
        response = LLMResponse(
            text_response="Response",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )

        assert response.usage is not None
        assert response.usage["prompt_tokens"] == 10
        assert response.usage["completion_tokens"] == 5

    def test_invalid_text_response(self):
        """Test that non-string text_response raises ValueError."""
        with pytest.raises(ValueError):
            LLMResponse(text_response=123)  # type: ignore[arg-type]

    def test_invalid_tool_calls_list(self):
        """Test that non-list tool_calls raises ValueError."""
        with pytest.raises(ValueError):
            LLMResponse(text_response="Test", tool_calls="not a list")  # type: ignore[arg-type]


class TestToolCallError:
    """Tests for ToolCallError exception."""

    def test_tool_call_error_message(self):
        """Test ToolCallError with message."""
        error = ToolCallError("Tool call failed: invalid JSON")

        assert str(error) == "Tool call failed: invalid JSON"
        assert isinstance(error, Exception)
