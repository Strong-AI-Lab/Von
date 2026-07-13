"""Core data structures for structured tool calling.

Canonical types that all LLM clients and workflow executors use.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional
import uuid
from datetime import datetime, timezone


class ToolCallError(Exception):
    """Raised when a tool call fails or is invalid."""

    pass


class StructuredToolTransportError(ToolCallError):
    """Base error for an inspectable structured-tool transport failure."""

    failure_kind = "structured_tool_transport_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        decision: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.decision = dict(decision or {})


class UnsupportedStructuredToolTransportError(StructuredToolTransportError):
    """Raised before a request when represented profiles rule out tools."""

    failure_kind = "structured_tool_transport_unsupported"


class StructuredToolCapabilityRejectedError(StructuredToolTransportError):
    """Raised when a provider rejects the selected tool/API capability."""

    failure_kind = "structured_tool_capability_rejected"


class StructuredToolProtocolError(StructuredToolTransportError):
    """Raised when a provider-native call/result loop cannot be correlated."""

    failure_kind = "structured_tool_protocol_error"


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
    provider_item_id: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.tool_name or not isinstance(self.tool_name, str):
            raise ValueError("tool_name must be a non-empty string")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be a dict")


@dataclass(frozen=True)
class ToolResult:
    """Provider-neutral result correlated to a prior structured tool call."""

    call_id: str
    output: Any
    tool_name: Optional[str] = None
    status: str = "ok"

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id.strip():
            raise ValueError("call_id must be a non-empty string")
        if not isinstance(self.status, str) or not self.status.strip():
            raise ValueError("status must be a non-empty string")


@dataclass(frozen=True)
class LLMContinuation:
    """Opaque-but-serialisable context required for a provider tool loop.

    ``input_items`` and ``output_items`` retain ordered provider items for a
    stateless continuation.  They are transport context, never user-visible
    response text or an execution-policy surface.
    """

    provider: str
    api_surface: str
    model: str
    state_mode: str = "stateless"
    response_id: Optional[str] = None
    connection_id: Optional[str] = None
    deployment_id: Optional[str] = None
    input_items: List[Dict[str, Any]] = field(default_factory=list)
    output_items: List[Dict[str, Any]] = field(default_factory=list)
    transport_decision: Dict[str, Any] = field(default_factory=dict)

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "api_surface": self.api_surface,
            "model": self.model,
            "state_mode": self.state_mode,
            "response_id": self.response_id,
            "connection_id": self.connection_id,
            "deployment_id": self.deployment_id,
            "input_items": list(self.input_items),
            "output_items": list(self.output_items),
            "transport_decision": dict(self.transport_decision),
        }

    @classmethod
    def from_value(cls, value: Any) -> Optional["LLMContinuation"]:
        instance_value = value if isinstance(value, cls) else None
        if isinstance(value, cls):
            value = value.to_mapping()
        if not isinstance(value, Mapping):
            return None
        provider = value.get("provider")
        api_surface = value.get("api_surface")
        model = value.get("model")
        if not all(
            isinstance(item, str) and item.strip()
            for item in (provider, api_surface, model)
        ):
            return None
        state_mode = value.get("state_mode", "stateless")
        if state_mode not in {"stateless", "provider_managed"}:
            return None
        response_id = value.get("response_id")
        connection_id = value.get("connection_id")
        deployment_id = value.get("deployment_id")
        if response_id is not None and not isinstance(response_id, str):
            return None
        if connection_id is not None and not isinstance(connection_id, str):
            return None
        if deployment_id is not None and not isinstance(deployment_id, str):
            return None
        raw_input_items = value.get("input_items", [])
        raw_output_items = value.get("output_items", [])
        for raw_items in (raw_input_items, raw_output_items):
            if not isinstance(raw_items, list) or not all(
                isinstance(item, Mapping) for item in raw_items
            ):
                return None
        raw_transport_decision = value.get("transport_decision", {})
        if not isinstance(raw_transport_decision, Mapping):
            return None
        if instance_value is not None:
            return instance_value
        return cls(
            provider=str(provider),
            api_surface=str(api_surface),
            model=str(model),
            state_mode=state_mode,
            response_id=response_id,
            connection_id=connection_id,
            deployment_id=deployment_id,
            input_items=[dict(item) for item in raw_input_items],
            output_items=[dict(item) for item in raw_output_items],
            transport_decision=dict(raw_transport_decision),
        )


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
    continuation: Optional[LLMContinuation] = None
    transport_metadata: Dict[str, Any] = field(default_factory=dict)

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
        if not isinstance(self.transport_metadata, dict):
            raise ValueError("transport_metadata must be a dict")
