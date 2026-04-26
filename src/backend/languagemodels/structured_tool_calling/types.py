"""Core data structures for structured tool calling.

Canonical types that all LLM clients and workflow executors use.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import uuid
from datetime import datetime, timezone


class ToolCallError(Exception):
    """Raised when a tool call fails or is invalid."""

    pass


@dataclass(frozen=True)
class ToolDefinition:
    """Definition of a tool available to the LLM.

    Attributes:
        name: Tool identifier (e.g., 'get_concept_details')
        description: Human-readable description of what the tool does
        input_schema: JSON Schema describing input parameters
        output_schema: Optional JSON Schema describing output structure
                      (used by workflows for validation)
    """

    name: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Tool name must be a non-empty string")
        if not self.description or not isinstance(self.description, str):
            raise ValueError("Tool description must be a non-empty string")
        if not isinstance(self.input_schema, dict):
            raise ValueError("input_schema must be a dict (JSON Schema)")


@dataclass(frozen=True)
class ToolCall:
    """Structured representation of an LLM tool invocation.

    This is the canonical internal format used across all providers.
    The call_id enables tracing through workflow execution traces.

    Attributes:
        tool_name: Name of the tool to invoke (matches ToolDefinition.name)
        payload: Tool input arguments (validated against ToolDefinition.input_schema)
        call_id: Unique identifier for execution tracing. Generated if not provided.
        timestamp: When the tool call was created (UTC)
    """

    tool_name: str
    payload: Dict[str, Any]
    call_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.tool_name or not isinstance(self.tool_name, str):
            raise ValueError("tool_name must be a non-empty string")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be a dict")


@dataclass(frozen=True)
class LLMResponse:
    """Response from an LLM with optional tool calls.

    An LLM response is either:
    1. A text response (assistant's natural language reply)
    2. Tool calls (structured requests to invoke tools)
    3. Both (some models may include text + tool calls)

    Attributes:
        text_response: Natural language text from LLM (may be empty if tool calls present)
        tool_calls: List of structured tool invocations (empty if no tools called)
        raw_response: Original provider response for debugging
        model: Model identifier used for this response
        usage: Token usage info (if available from provider)
        tool_call_diagnostics: Provider-side tool-call parse/contract diagnostics.
    """

    text_response: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    raw_response: Optional[Dict[str, Any]] = None
    model: Optional[str] = None
    usage: Optional[Dict[str, Any]] = (
        None  # e.g., {"prompt_tokens": 100, "completion_tokens": 50}
    )
    tool_call_diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    def has_tool_calls(self) -> bool:
        """Return True if this response contains tool calls."""
        return len(self.tool_calls) > 0

    def __post_init__(self):
        if not isinstance(self.text_response, str):
            raise ValueError("text_response must be a string")
        if not isinstance(self.tool_calls, list):
            raise ValueError("tool_calls must be a list")
        if not isinstance(self.tool_call_diagnostics, list):
            raise ValueError("tool_call_diagnostics must be a list")
